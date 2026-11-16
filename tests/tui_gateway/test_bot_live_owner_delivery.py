"""Imported turns retain their receipt and cannot bypass the local FIFO."""
import contextlib
import threading
from types import SimpleNamespace

from tui_gateway.method_ctx import rebind
from tui_gateway import session_notifications, session_auto_continue
from tui_gateway.turn_marker import record_turn_start, read_turn_marker


def test_refused_input_reports_a_failed_terminal_outcome():
    import contextlib
    import contextvars
    import logging
    import time
    from tui_gateway import prompt_turn
    from tui_gateway.session_lifecycle import _prepare_session_work

    agent = SimpleNamespace(session_id="chat")
    session = dict(agent=agent, session_key="chat", history_lock=threading.RLock(), running=True)
    retired = []
    noop = lambda *args, **kwargs: None
    worker_errors = []
    # server installs a process-wide excepthook during the worker's lazy import;
    # capture at the worker boundary rather than relying on that mutable hook.
    def prepare_work(target, **kwargs):
        def run():
            try:
                target()
            except BaseException as exc:
                worker_errors.append(exc)
        return _prepare_session_work(run, **kwargs)

    submit = rebind(prompt_turn._run_admitted_prompt, {
        "threading": threading, "time": time, "logger": logging.getLogger(__name__),
        "_prepare_session_work": prepare_work,
        "_sessions_lock": threading.RLock(), "_sessions": {},
        "_admit_prompt_turn": lambda *args, **kwargs: ([], agent),
        "_session_profile_runtime_scope": lambda session: contextlib.nullcontext(),
        "_emit": noop, "bind_transport": noop, "reset_transport": noop,
        "_current_runtime_session_record": contextvars.ContextVar("refused_turn"),
        "_TurnRun": prompt_turn._TurnRun,
        "_record_turn_marker": lambda *args, **kwargs: "marker",
        "_prepare_turn_input": lambda *args: None,
        "_release_turn_scopes": noop, "_post_turn_housekeeping": noop,
        "_rearm_carried_pause_watcher": noop,
        "_clear_inflight_turn": noop,
        # Hosted room member sessions drop their bot_room slot at turn end (#106847); a canonical chat is not one.
        "_release_hosted_room_turn_slot": noop,
        "_retire_turn_marker": lambda *args, **kwargs: retired.append(args),
        "_emit_settled_session_info": noop,
        "_routing_provenance_db": lambda _session: contextlib.nullcontext(None),
        "_reopen_routed_session_row": noop,
        # Every dispatch binds the session's own row before the turn writes (#111999).
        "_ensure_session_db_row": noop,
        # [daveey] Server-bound commit point for synthesized turns (exit_job_receipts); a user turn passes through.
        "start_synthesized_turn": session_notifications.start_synthesized_turn,
        "admit_synthesized_turn": session_notifications.admit_synthesized_turn,
    })
    outcomes = []
    assert submit(None, "live", session, "refused input", terminal_callback=outcomes.append)
    # The turn thread lazily imports ``tui_gateway.server`` (seconds on a CPU-throttled runner); join returns as
    # soon as it ends, and a timeout here would strand a half-imported module that errors every later fixture.
    session["_run_thread"].join(timeout=180)
    assert not session["_run_thread"].is_alive()
    assert not worker_errors, [str(error) for error in worker_errors]
    assert [outcome["status"] for outcome in outcomes] == ["failed"]
    assert retired and session["running"] is False


def test_imported_crash_marker_never_autocontinues(tmp_path):
    record_turn_start(tmp_path, "chat", "imported", auto_continue=False)
    marker = read_turn_marker(tmp_path, "chat")
    assert marker["auto_continue"] is False
    schedule = rebind(session_auto_continue._maybe_schedule_auto_continue, {
        "_session_home": lambda session: tmp_path,
        "read_turn_marker": read_turn_marker,
    })
    assert schedule("live", {}, "chat") is None


def test_viewer_poller_never_discovers_or_claims_bot_execution(monkeypatch, tmp_path):
    import queue
    import time
    import tools.bot_live_delivery as mailbox
    from tools.process_registry import process_registry
    calls = []
    monkeypatch.setattr(mailbox, 'find_canonical_live_owner', lambda home: calls.append(home))
    stop = threading.Event()
    events = queue.Queue()
    events.put({'type': 'owned-completion'})
    monkeypatch.setattr(process_registry, 'completion_queue', events)
    delivered = []
    import logging

    def unexpected_failure(what, exc):
        raise AssertionError(f"{what}: {exc}") from exc

    # Server installation rebinds helpers too; a shallow copy of module globals does not.
    release_claims = rebind(session_notifications._kb_release_claims, {
        '_notif_log_failure': unexpected_failure, 'logger': logging.getLogger(__name__),
    })
    # Start from the module's own globals: the fork's poller also reads its parked/held handoff helpers.
    globals_ = {
        **vars(session_notifications),
        'logger': logging.getLogger(__name__),
        '_kb_release_claims': release_claims, '_notif_log_failure': unexpected_failure,
        'time': time, '_LOOP_POLL_SECONDS': 0, '_KANBAN_POLL_SECONDS': 0,
        '_poll_bot_live_delivery_once': lambda *a: calls.append('legacy-claim'),
        '_maybe_fire_tui_loop_tick': lambda *a: None,
        '_maybe_fire_tui_heartbeat_tick': lambda *a: None,
        '_notif_poll_kanban': lambda *a: None,
        '_session_profile_runtime_scope': lambda session: contextlib.nullcontext(),
        '_session_home': lambda session: tmp_path,
        '_notif_handle_ready': lambda sid, session, ready, *a, **kw: (delivered.extend(ready), stop.set()),
    }
    for name in ('_notification_poller_run', '_notification_poller_cleanup'):
        globals_[name] = rebind(getattr(session_notifications, name), globals_)
    poll = rebind(session_notifications._notification_poller_scoped_loop, globals_)
    poll(stop, 'live', {'agent': object(), 'history_lock': threading.RLock(), 'session_key': 'chat',
                      'active_session_lease': SimpleNamespace(lease_id='lease', released=False)})
    assert delivered == [{'type': 'owned-completion'}]
    assert calls == []
