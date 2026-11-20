"""Idempotent admission on the existing owning runtime, never a second worker.

Only idle, already-loaded inline sessions are supported. Lost-response retries
and readback work without a live owner. An accepted operation is never dispatched
again, even if the process died between the SQLite commit and thread start.
"""
from pathlib import Path
from contextlib import ExitStack, contextmanager
import threading
import time
import logging

from hermes_state_prompt_admission import admit_prompt, mark_dispatch_failed, prompt_digest, read_admission


_operations_lock = threading.Lock()
_operations = {}
_BUSY = object()


@contextmanager
def _operation_lock(home, operation_id):
    # Serialize only retries of this operation, never unrelated sessions or DB work.
    key = (home, operation_id)
    with _operations_lock:
        entry = _operations.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _operations_lock:
            entry[1] -= 1
            if not entry[1]:
                del _operations[key]


def _owner_home(server, profile):
    # Unlike legacy resume, this API does not accept aliases for a profile.
    if server._response_profile_name(profile) != profile:
        raise ValueError("exact canonical profile required")
    return Path(server._profile_home(profile) or server._hermes_home).resolve()


def _read(server, params, home):
    # A failed cancellation write keeps the live writer quarantined. Readback
    # retries retirement, NEVER scheduling, before it reports a terminal result.
    with server._sessions_lock:
        sessions = list(server._sessions.values())
    for session in sessions:
        pending = session.get('_admission_cancel_pending')
        if (pending and pending['operation_id'] == params['operation_id']
                and session.get('session_key') == params['stored_session_id']
                and Path(session.get('profile_home') or server._hermes_home).resolve() == home):
            with pending['lock']:
                if session.get('_admission_cancel_pending') is pending:
                    _finish_cancellation(server, session, pending)
    with server._session_db({"profile_home": str(home) if home != Path(server._hermes_home).resolve() else None}) as db:
        if db is None:
            raise RuntimeError("session storage unavailable")
        return read_admission(db, params["stored_session_id"], params["operation_id"], params["payload_digest"])


def _reply(server, rid, params, receipt):
    return server._ok(rid, {**(receipt or {
        "status": "absent", "stored_session_id": params["stored_session_id"],
        "operation_id": params["operation_id"], "payload_digest": params["payload_digest"],
        "execution_status": "unknown"}), "profile": params["profile"]})


def get_admission(rid, params):
    from tui_gateway import server
    if server.current_transport() is not None:
        return server._err(rid, 4031, 'Prompt admission is restricted to in-process owner services')
    try:
        home = _owner_home(server, params["profile"])
        with _operation_lock(home, params['operation_id']):
            return _reply(server, rid, params, _read(server, params, home))
    except ValueError as exc:
        return server._err(rid, 4092, str(exc))


def _live_owner(server, params, home):
    with server._sessions_lock:
        matches = [(sid, s) for sid, s in server._sessions.items()
                   if s.get("session_key") == params["stored_session_id"]
                   and Path(s.get("profile_home") or server._hermes_home).resolve() == home]
    if len(matches) != 1:
        raise ValueError("exact session has no unique live owner on this runtime")
    sid, session = matches[0]
    if session.get("lazy") or server._submit_row_target_key(session) != params["stored_session_id"]:
        raise ValueError("exact session is lazy or rotated")
    if (session.get("running") or session.get("_closing") or session.get("queued_prompt")
            or session.get("queued_prompts") or session.get("attached_images")):
        raise ValueError("exact session is busy or has pending input; retry later with the same operation")
    if server._session_uses_compute_host(session, server._load_dashboard_process_isolation_config()):
        raise ValueError("exact admission does not support isolated compute sessions")
    if session.get("seeded") and not session.get("_branch_seed_persisted"):
        raise ValueError("exact admission does not support unpersisted branches")
    return sid, session


def _dispatch(server, rid, sid, session, text, profile):
    # Existing build/turn implementation adopts _submit_user_row and retains the
    # owner's transport. A plugin RPC must not steal the Desktop's connection.
    if not server._restart_completed_failed_agent_build(sid, session, session.get("agent_ready")):
        server._start_agent_build(sid, session)
    gate = session['_admission_dispatch_gate']
    staged = session['_submit_user_row']
    user_event = {
        'stored_session_id': staged['_submit_row_session_id'],
        'profile': profile,
        'user_row_id': staged['_row_id'],
        'message_uid': staged['message_uid'],
        'text': staged['content'],
        'timestamp': staged['timestamp'],
    }

    def run():
        with gate['lock']:
            if gate['cancelled']:
                return
            gate['started'] = True
        # Only the committed transaction winner projects this row, before any
        # model output. An unstarted cancellation must never paint permission.
        server._emit("message.admitted", sid, user_event)
        # Dispatch is never replayed, but a turn a restart stops BEFORE its first tool ran has done nothing
        # irreversible: its marker stays auto-continuable until then (turn_marker.mark_turn_tool_started), so the
        # next boot continues the decision instead of dropping it (self-heal desktop-3a6213ce74).
        with session["history_lock"]:
            session["_marker_until_tool"] = True
            session.pop("_submit_input_origin", None)  # [daveey] never a prior client submit's origin
        server._run_after_agent_ready(rid, sid, session, text, None, None, None, auto_continue=True)

    worker = server._start_session_work(run, name="prompt-admission", session=session)
    if worker is None:
        raise RuntimeError("backend retired after durable acceptance")


def _claim_turn(server, rid, params, home, text):
    """Under the resume lock: ``(refusal | _BUSY | None, sid, session)``; None = the turn is claimed (running)."""
    with server._session_resume_lock:
        sid, session = _live_owner(server, params, home)
        # busy_wait_s=0: never wait out a busy update lock while holding the resume lock (_BUSY: wait outside it).
        with server._session_turn_admission(session, busy_wait_s=0) as admitted:
            if not admitted:
                from hermes_cli.backend_retirement import last_refusal
                return (_BUSY if last_refusal() == "busy" else server._err(rid, 5035, "backend is retiring")), sid, session
            _live_owner(server, params, home)
            refusal = server._reattach_refusal(rid, sid, session)
            if refusal is not None:
                return refusal, sid, session
            if server._ws_session_is_detached(session):
                raise ValueError("exact session is detached; reattach its owner first")
            refusal = server._legacy_group_fence_error(rid, session, params)
            if refusal is not None:
                return refusal, sid, session
            session["running"] = True
            session["_turn_cancel_requested"] = False
            session["last_active"] = time.time()
            server._start_inflight_turn(session, text)
    return None, sid, session


def submit_admission(rid, params):
    from tui_gateway import server
    from hermes_cli.input_sanitize import sanitize_user_prompt_text

    if server.current_transport() is not None:
        return server._err(rid, 4031, 'Prompt admission is restricted to in-process owner services')
    try:
        home = _owner_home(server, params["profile"])
        text = params["text"]
        if not text.strip() or sanitize_user_prompt_text(text) != text:
            raise ValueError("text must be nonempty sanitized user input")
        if prompt_digest(params["stored_session_id"], text) != params["payload_digest"]:
            raise ValueError("payload digest mismatch")
        with _operation_lock(home, params["operation_id"]):
            # Serialize readback with scheduling and cancellation: durable
            # acceptance alone is not yet a settled delivery result.
            # Read outside the process/history locks: SQLite can wait on a writer.
            prior = _read(server, params, home)
            if prior is not None:
                return _reply(server, rid, params, prior)
            with ExitStack() as waiting:
                outcome, sid, session = _claim_turn(server, rid, params, home, text)
                if outcome is _BUSY:
                    # [daveey] A briefly busy update lock (a hub Kanban claim's ~1 s sample, review F1) is waited out
                    # with the resume lock released, then admission is retried once. Nothing durable happened yet
                    # and the operation lock is still held, so the retry cannot duplicate this turn.
                    from hermes_cli.backend_retirement import admission_waiter, refusal_text
                    if waiting.enter_context(admission_waiter(session.get("profile_home"))):
                        outcome, sid, session = _claim_turn(server, rid, params, home, text)
                    if outcome is _BUSY:
                        return server._err(rid, 5035, refusal_text())
                if outcome is not None:
                    return outcome
            return _accept_claimed(server, rid, sid, session, params, text)
    except ValueError as exc:
        return server._err(rid, 4092, str(exc))
    except Exception as exc:
        logging.getLogger(__name__).exception("Prompt admission failed: %s", params["operation_id"])
        return server._err(rid, 5071, f"Prompt admission storage failed; query this operation before retrying: {exc}")


def _undo_claim(server, session, *, retain_error=False):
    # Keep running visible until release completes so another admission cannot
    # inherit a lease which this failed turn is about to release.
    try:
        server._release_active_session_slot(session)
    finally:
        with session["history_lock"]:
            session["running"] = False
            session["last_active"] = time.time()
            session.pop("_submit_user_row", None)
            if not retain_error:
                server._clear_inflight_turn(session)


def _finish_cancellation(server, session, params):
    with server._session_db(session) as db:
        receipt = mark_dispatch_failed(db, params['stored_session_id'],
                                       params['operation_id'], params['payload_digest'])
    # Persist retirement BEFORE releasing the writer: the next ordinary prompt
    # must never load this cancelled approval from active history.
    _undo_claim(server, session, retain_error=True)
    session.pop('_admission_cancel_pending', None)
    return receipt


def _accept_claimed(server, rid, sid, session, params, text):
    try:
        if (limit := server._ensure_active_session_slot(sid, session, allow_takeover=False)) is not None:
            _undo_claim(server, session)
            return server._err(rid, 4090, str(limit))
        with server._session_db(session) as db:
            if db is None:
                raise RuntimeError("session storage unavailable")
            receipt, staged, created = admit_prompt(
                db, params["stored_session_id"], params["operation_id"], text, params["payload_digest"])
    except Exception:
        _undo_claim(server, session)
        raise
    if not created:
        _undo_claim(server, session)
        return _reply(server, rid, params, receipt)
    gate = {'lock': threading.Lock(), 'started': False, 'cancelled': False}
    with session["history_lock"]:
        session["_submit_user_row"] = staged
        session['_admission_dispatch_gate'] = gate
    try:
        if session.get("_turn_cancel_requested") or session.get("_closing") or not session.get("running"):
            raise RuntimeError("turn cancelled after durable acceptance")
        _dispatch(server, rid, sid, session, text, params['profile'])
    except Exception as exc:
        server.logger.exception("Prompt accepted but dispatch failed: %s", params["operation_id"])
        with gate['lock']:
            if gate['started']:
                # The worker owns cleanup now. Never claim cancellation or release
                # its writer after an ambiguous scheduler return.
                return _reply(server, rid, params, receipt)
            gate['cancelled'] = True
        pending = {**params, 'lock': threading.Lock()}
        session['_admission_cancel_pending'] = pending
        try:
            # [daveey] retire_marker=False: the dispatch never started a turn, so no marker is this one's.
            server._emit_terminal_turn_error(sid, session, exc, error_surface={
                "layer": "runtime", "code": "admission_dispatch_failed", "retryable": False}, retire_marker=False)
        finally:
            try:
                with pending['lock']:
                    receipt = _finish_cancellation(server, session, pending)
            except Exception:
                server.logger.exception("Could not retire failed dispatch; writer remains quarantined")
                raise
    return _reply(server, rid, params, receipt)


def register(server):
    server.register_method("prompt.admit", server._profile_scoped(submit_admission))
    server.register_method("prompt.admission.get", server._profile_scoped(get_admission))
