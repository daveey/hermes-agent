"""[daveey] A core-update pause in a legacy backend while the gateway's ledger owns the chat (t_01a1d4920ce3cbf4).

David-MBP 2026-11-13 (PST): a kanban-notice turn in Desktop backend pid 81192 paused for an update at 00:55; the
update did not apply. From 00:58 to 02:00 that writer's continuation was refused every 2-3 min ("Refusing legacy
turn ... canonical ledger holds queued work" -> "refused: session has another live owner") while the gateway ran 9
turns of the same chat, and the other backends (pids 6243, 4658) held back because the marker's writer was alive.
Nobody continued or retired the pause. At 02:24, on the next backend, it was replayed with "Your previous turn was
interrupted mid-run", quoting the 2 h old notice that a later turn had already handled.

Contract pinned here:
- A paused continuation refused by the gateway's ledger goes to the owner's FIFO exactly once (no legacy turn), and
  the pause is retired here, within one watcher poll of the window's end plus the kickoff (build wait, admit
  receipt). Work no running gateway will finish, or an owner that definitively refuses it, retires the pause with a
  logged reason instead of waiting out the freshness window; an owner that turns it away for a passing reason
  (nothing queued) keeps the 30 s backoff, up to OWNER_FORWARD_TRIES forwards per pause.
- "window ended without an update" is logged once per pause, however many watchers re-arm on it.
- A continuation that runs after later turns ran over the pause never says the previous turn was interrupted: it
  names when the pause happened and that later turns ran since.
"""
from __future__ import annotations

import logging
import threading
import time
import types

import pytest

from hermes_cli.active_sessions import ActiveSessionRefusal, SESSION_NOT_OWNED
from hermes_state import SessionDB
from tui_gateway import owner_forward, server
from tui_gateway import update_pause as up
from tui_gateway.session_history import _legacy_display_kind
from tui_gateway.turn_marker import mark_turn_paused, read_turn_marker, record_turn_start

CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True, "freshness_minutes": 240,
                                     "max_attempts": 2}}}
PROMPT = "[Kanban notice] t_01a1d3efb167bf46 done"
REAL_FORWARD = owner_forward.forward_prompt  # the env fixture replaces it; receipt tests drive it


class _InlineThread:
    def __init__(self, target=None, daemon=None, args=(), kwargs=None, name=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def _session(home, **extra):
    return {"agent": types.SimpleNamespace(), "session_key": "chat", "history": [], "profile_home": str(home),
            "history_lock": threading.RLock(), "history_version": 0, "running": False, "attached_images": [],
            "image_counter": 0, "cols": 80, "slash_worker": None, "show_reasoning": False,
            "tool_progress_mode": "all", "inflight_turn": None, **extra}


def _ledger_refusal(status="queued"):
    # What session_lifecycle._canonical_ledger_refusal returns while the gateway's ledger holds work.
    refusal = ActiveSessionRefusal("Session chat has pending work in the gateway's durable queue.", SESSION_NOT_OWNED)
    refusal.ledger_status = status
    return refusal


@pytest.fixture()
def env(monkeypatch, tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("chat", source="desktop")
    db.append_message("chat", "user", PROMPT)
    db.append_message("chat", "assistant", "step one done")
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: CFG)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_profile_session_db", lambda home: (db, False))
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_wait_agent", lambda session, rid, timeout=30.0: None)
    ledger = {"status": "queued"}
    monkeypatch.setattr(server, "_ensure_active_session_slot",
                        lambda sid, session, **kw: _ledger_refusal(ledger["status"]) if ledger["status"] else None)
    ran, forwarded = [], []
    monkeypatch.setattr(server, "_run_prompt_submit", lambda rid, sid, s, text, **kw: ran.append(text) or True)
    monkeypatch.setattr(owner_forward, "forward_prompt",
                        lambda sid, s, text, **kw: forwarded.append(text) or {"admission_id": "adm-1"})
    record_turn_start(tmp_path, "chat", PROMPT)
    mark_turn_paused(tmp_path, "chat", up.PAUSE_REASON)
    yield types.SimpleNamespace(home=tmp_path, db=db, ledger=ledger, ran=ran, forwarded=forwarded)
    db.close()


def _shift_pause(home, seconds):
    """Move the stored pause stamp by ``seconds`` (a distinct, earlier or later pause without a wall-clock wait)."""
    import json
    path = home / "desktop" / "interrupted_turns.json"
    entries = json.loads(path.read_text())
    entries["chat"]["paused_at"] += seconds
    path.write_text(json.dumps(entries))


def _watch(monkeypatch, session, *, max_s=120.0):
    """Run the real watcher (inline thread, fake clock) with the window already over."""
    clock = [0.0]
    monkeypatch.setattr(up, "_detect_skew", lambda: None)
    monkeypatch.setattr(up.threading, "Thread", _InlineThread)
    monkeypatch.setattr(up, "time", types.SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s), time=time.time))
    monkeypatch.setattr(up, "WATCH_MAX_S", max_s)
    up.start_in_place_watcher("sid", session, "chat")
    return clock[0]


def test_gateway_owned_pause_goes_to_the_owner_once_and_is_retired_here(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    session = _session(env.home)

    elapsed = _watch(monkeypatch, session)

    assert env.ran == []  # no legacy turn beside the canonical FIFO
    assert len(env.forwarded) == 1, "the paused continuation never reached the chat's owner"
    assert env.forwarded[0].endswith(PROMPT)
    assert read_turn_marker(env.home, "chat") is None  # retired: the readers stop deferring to this writer
    assert elapsed <= up.WATCH_POLL_S  # the first poll after the window ended
    assert "queued in the FIFO of the gateway that owns the chat (admission adm-1)" in caplog.text
    assert not session.get("_auto_continue_scheduled") and not session.get("running")

    # Later watchers (a carried turn's re-arm, a resume) find nothing to continue: still exactly one.
    _watch(monkeypatch, session)
    server._maybe_schedule_auto_continue("sid", session, "chat")
    assert len(env.forwarded) == 1 and env.ran == []


def test_forwarded_continuation_is_never_worded_as_the_previous_turn(env, monkeypatch):
    _watch(monkeypatch, _session(env.home))
    note = env.forwarded[0]
    assert not note.startswith("[System note: Your previous turn")
    assert note.startswith(up.PAUSE_REPLAY_NOTE_PREFIX)
    assert "queued behind other turns" in note
    assert _legacy_display_kind("user", note) == "auto_continue"  # the owner persists it untyped


@pytest.mark.parametrize("failure", ["unknown", "not_running", "unconfirmed", "permission_denied", "bug"])
def test_owner_that_will_not_run_it_retires_the_pause_with_a_reason(env, monkeypatch, caplog, failure):
    caplog.set_level(logging.INFO)
    if failure == "unknown":
        env.ledger["status"] = "unknown"  # only Discard resolves an outcome-unknown admission
    else:
        from hermes_cli.gateway_client import GatewayRPCError
        exc = {"not_running": owner_forward.OwnerNotRunning("no gateway serves this home"),
               "unconfirmed": owner_forward.OwnerUnconfirmed("the gateway did not confirm it in time"),
               # A refusal that names the request itself: no later forward of the same request clears it.
               "permission_denied": GatewayRPCError("permission_denied", data={"reason": "permission_denied"}),
               "bug": KeyError("admission_id")}[failure]

        def refuse(sid, s, text, **kw):
            raise exc
        monkeypatch.setattr(owner_forward, "forward_prompt", refuse)
    session = _session(env.home)

    _watch(monkeypatch, session)

    assert env.ran == []
    assert read_turn_marker(env.home, "chat") is None
    retired = [r for r in caplog.records if "retired the paused turn of chat" in r.getMessage()]
    assert len(retired) == 1 and retired[0].levelno == logging.WARNING
    assert "Paused request" in retired[0].getMessage()


def _transient(kind):
    from hermes_cli.gateway_client import GatewayRPCError
    return {"not_admitted": owner_forward.OwnerUnavailable("the gateway did not admit it"),
            "queue_full": GatewayRPCError("queue_full", data={"reason": "queue_full"}),
            "draining": GatewayRPCError("runtime_draining", data={"reason": "runtime_draining"}),
            "dial": ConnectionRefusedError(111, "Connection refused")}[kind]


@pytest.mark.parametrize("kind", ["not_admitted", "queue_full", "draining", "dial"])
def test_transient_forward_failure_keeps_the_backoff_then_retires_after_the_cap(env, monkeypatch, caplog, kind):
    """PR #2167 review: an owner that turned the forward away for a passing reason (nothing queued there) keeps the
    pause and the 30 s backoff, so the next poll forwards again; only OWNER_FORWARD_TRIES such failures retire it."""
    caplog.set_level(logging.INFO)
    tries = []

    def refuse(sid, s, text, **kw):
        tries.append(text)
        raise _transient(kind)
    monkeypatch.setattr(owner_forward, "forward_prompt", refuse)
    session = _session(env.home)

    for attempt in range(1, server.OWNER_FORWARD_TRIES):
        server._maybe_schedule_auto_continue("sid", session, "chat")
        assert len(tries) == attempt and env.ran == []
        assert read_turn_marker(env.home, "chat") is not None, "a transient owner failure retired the pause"
        assert session["_auto_continue_retry_after"] > time.monotonic() + 20.0
        assert not session.get("_auto_continue_scheduled") and not session.get("running")
        assert f"kept for a later try after the 30s backoff (attempt {attempt}/{server.OWNER_FORWARD_TRIES})" in caplog.text
        server._maybe_schedule_auto_continue("sid", session, "chat")  # inside the backoff: no forward
        assert len(tries) == attempt
        session["_auto_continue_retry_after"] = 0.0  # the backoff passed

    server._maybe_schedule_auto_continue("sid", session, "chat")

    assert len(tries) == server.OWNER_FORWARD_TRIES and env.ran == []
    assert read_turn_marker(env.home, "chat") is None
    retired = [r for r in caplog.records if "retired the paused turn of chat" in r.getMessage()]
    assert len(retired) == 1 and f"({server.OWNER_FORWARD_TRIES} attempts)" in retired[0].getMessage()


def test_transient_forward_failure_then_success_hands_over_once(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    answers = [_transient("not_admitted"), {"admission_id": "adm-2"}]

    def forward(sid, s, text, **kw):
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        env.forwarded.append(text)
        return answer
    monkeypatch.setattr(owner_forward, "forward_prompt", forward)
    session = _session(env.home)

    server._maybe_schedule_auto_continue("sid", session, "chat")
    assert env.forwarded == [] and read_turn_marker(env.home, "chat") is not None
    session["_auto_continue_retry_after"] = 0.0
    server._maybe_schedule_auto_continue("sid", session, "chat")

    assert len(env.forwarded) == 1 and env.ran == [] and answers == []
    assert read_turn_marker(env.home, "chat") is None
    assert "queued in the FIFO of the gateway that owns the chat (admission adm-2)" in caplog.text


@pytest.mark.parametrize("receipt, retried", [({"admission_id": "adm-9", "status": "unknown"}, False),
                                               ({"status": "unknown"}, True)])
def test_a_receipt_naming_an_admission_is_never_resent(env, monkeypatch, caplog, receipt, retried):
    """PR #2265 review: an owner receipt outside queued/started/terminal that still names an admission_id proves a
    durable row there (AdmissionReceipt.status allows 'unknown'); a resend carries a fresh submission_id and would
    queue a second copy. So it is unconfirmed (retired at once), and only a receipt naming no admission is the
    transient 'did not admit it'. Real forward_prompt and relay; only the owner's discovery and socket are fakes."""
    import contextlib
    caplog.set_level(logging.INFO)
    submits = []

    class _Owner:
        events = None

        async def rpc(self, method, **params):
            if method == "session.resume":
                return {"session_id": "chat"}
            submits.append(params["submission_id"])
            return dict(receipt)

    @contextlib.asynccontextmanager
    async def connect(_home, _endpoint):
        yield _Owner()
    monkeypatch.setattr(owner_forward, "forward_prompt", REAL_FORWARD)
    monkeypatch.setattr(owner_forward, "discover_owner", lambda _home: "ready-owner")
    monkeypatch.setattr(owner_forward, "connect_owner", connect)
    session = _session(env.home)

    server._maybe_schedule_auto_continue("sid", session, "chat")

    assert len(submits) == 1 and env.ran == []
    if retried:
        assert read_turn_marker(env.home, "chat") is not None
        assert "did not admit it" in caplog.text and "kept for a later try" in caplog.text
    else:
        assert read_turn_marker(env.home, "chat") is None
        retired = [r for r in caplog.records if "retired the paused turn of chat" in r.getMessage()]
        assert len(retired) == 1 and "may be queued there" in retired[0].getMessage()
        session["_auto_continue_retry_after"] = 0.0
        server._maybe_schedule_auto_continue("sid", session, "chat")
        assert len(submits) == 1, "a forward the owner recorded was sent again"


def test_a_new_pause_gets_a_fresh_forward_budget(env, monkeypatch):
    def refuse(sid, s, text, **kw):
        raise _transient("not_admitted")
    monkeypatch.setattr(owner_forward, "forward_prompt", refuse)
    session = _session(env.home)
    session["_owner_forward_failures"] = (("chat", 0.0, 0.0, 1), server.OWNER_FORWARD_TRIES)  # an older pause's
    server._maybe_schedule_auto_continue("sid", session, "chat")
    assert read_turn_marker(env.home, "chat") is not None
    assert session["_owner_forward_failures"][1] == 1


def test_other_owner_refusals_keep_the_existing_backoff(env, monkeypatch):
    """A sibling backend mid-turn (no ledger status) still backs off and keeps the marker, as before."""
    monkeypatch.setattr(server, "_ensure_active_session_slot",
                        lambda sid, session, **kw: ActiveSessionRefusal("held", SESSION_NOT_OWNED))
    session = _session(env.home)
    server._maybe_schedule_auto_continue("sid", session, "chat")
    assert env.forwarded == [] and env.ran == []
    assert read_turn_marker(env.home, "chat") is not None
    assert session["_auto_continue_retry_after"] > time.monotonic() + 20.0


def test_handoff_does_not_retire_a_newer_pause(env, monkeypatch):
    session = _session(env.home)
    marker = read_turn_marker(env.home, "chat")
    mark_turn_paused(env.home, "chat", up.PAUSE_REASON)  # paused again meanwhile: a new pause
    _shift_pause(env.home, 1.0)
    assert server._paused_turn_to_ledger_owner("sid", session, "chat", marker, _ledger_refusal()) is True
    assert env.forwarded == []  # the snapshot is stale: nothing handed over
    assert read_turn_marker(env.home, "chat") is not None


def test_window_end_is_announced_once_per_pause_across_rearmed_watchers(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="tui_gateway.update_pause")
    env.ledger["status"] = None  # admitted: schedule returns at once without starting (busy session below)
    session = _session(env.home, _auto_continue_retry_after=float("inf"))  # every schedule attempt backs off
    for _ in range(3):  # e.g. three carried notice turns ending, each re-arming a watcher
        _watch(monkeypatch, session, max_s=4 * up.WATCH_POLL_S)
    lines = [r for r in caplog.records if "window ended without an update" in r.getMessage()]
    assert len(lines) == 1

    # A new pause of the turn is announced again.
    mark_turn_paused(env.home, "chat", up.PAUSE_REASON)
    _shift_pause(env.home, 1.0)
    _watch(monkeypatch, session, max_s=2 * up.WATCH_POLL_S)
    assert len([r for r in caplog.records if "window ended without an update" in r.getMessage()]) == 2


def test_continuation_after_later_turns_names_the_pause_and_the_turns_since(env, monkeypatch):
    """Test 2 of the card: a pause, then completed later turns, then the continuation runs here."""
    env.ledger["status"] = None
    paused_at = read_turn_marker(env.home, "chat")["paused_at"]
    for notice in ("[Kanban notice] t_a done", "[Kanban notice] t_b done"):  # notice turns carried over the pause
        env.db.append_message("chat", "user", notice)
        env.db.append_message("chat", "assistant", "handled")
    session = _session(env.home)

    server._maybe_schedule_auto_continue("sid", session, "chat")

    assert len(env.ran) == 1
    note = env.ran[0]
    assert "interrupted" not in note.split("]")[0]
    assert not note.startswith("[System note: Your previous turn")
    assert "2 later turns ran in this chat since" in note
    assert up._clock_label(paused_at) in note and " ago)" in note
    assert "don't redo finished work" in note and note.endswith(PROMPT)
    assert _legacy_display_kind("user", note) == "auto_continue"


def test_continuation_right_after_the_pause_keeps_the_previous_turn_note(env, monkeypatch):
    env.ledger["status"] = None
    server._maybe_schedule_auto_continue("sid", _session(env.home), "chat")
    assert env.ran == [up.pause_note(PROMPT)]


def test_the_paused_turns_own_request_row_is_never_a_later_turn(env, monkeypatch):
    """The paused request's row stamped after the pause (a late step flush; the CI fixtures back-date the marker)
    is the paused turn itself, not a turn that ran over it: the note stays the previous-turn note."""
    env.ledger["status"] = None
    _shift_pause(env.home, -3 * up.STALE_NOTE_AFTER_S)  # the transcript rows now postdate the pause stamp
    server._maybe_schedule_auto_continue("sid", _session(env.home), "chat")
    assert env.ran == [up.pause_note(PROMPT)]


def test_continuation_that_paused_again_is_forwarded_once_and_named_as_a_chain(env, monkeypatch):
    """David-MBP 03:07-03:29 PST: the 02:24 continuation of the 00:55 pause paused again (``pauses: 2``, the marker
    keeping the chain's root prompt, the 00:33 notice). The ledger refused it while the gateway continued its own
    second pause record of the chat (admission fba3e145…), and at 03:29 it was replayed in place as if new."""
    from tui_gateway.turn_marker import clear_turn_marker
    clear_turn_marker(env.home, "chat")
    record_turn_start(env.home, "chat", PROMPT, pauses=1)  # the continuation keeps the root prompt
    mark_turn_paused(env.home, "chat", up.PAUSE_REASON)  # its own pause: pauses 2, paused_at = the 03:07 stamp
    marker = read_turn_marker(env.home, "chat")
    assert marker["pauses"] == 2
    env.ledger["status"] = "started"  # the gateway is running its own continuation of the chat's other pause
    env.db.append_message("chat", "user", "[IMPORTANT: 28 Kanban task updates arrived]")  # the gateway's turn
    env.db.append_message("chat", "assistant", "handled")
    session = _session(env.home)

    _watch(monkeypatch, session)
    server._maybe_schedule_auto_continue("sid", session, "chat")  # a later resume: nothing left to continue

    assert env.ran == [] and len(env.forwarded) == 1
    note = env.forwarded[0]
    assert note.startswith(up.PAUSE_REPLAY_NOTE_PREFIX) and "interrupted" not in note.split("]")[0]
    assert up._clock_label(marker["paused_at"]) in note
    assert "1 later turn ran in this chat since" in note
    assert "This request is not new: it already ran as a continuation after 1 earlier pause" in note
    assert note.endswith(PROMPT)
    assert read_turn_marker(env.home, "chat") is None


def test_old_pause_whose_later_turns_cannot_be_counted_is_worded_as_stale(monkeypatch):
    monkeypatch.setattr(up, "_now", lambda: 10_000.0)
    note = up.pause_note(PROMPT, paused_at=10_000.0 - 2 * 3600, later_turns=None)
    assert note.startswith(up.PAUSE_REPLAY_NOTE_PREFIX) and "2.0 h ago" in note
    assert "other turns may have run in this chat since" in note
    # A fresh pause with an unreadable count is still the previous turn.
    assert up.pause_note(PROMPT, paused_at=10_000.0 - 30, later_turns=None) == up.pause_note(PROMPT)
