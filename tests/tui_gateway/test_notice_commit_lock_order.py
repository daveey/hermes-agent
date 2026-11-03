"""A notice turn's exit-fence commit keeps the ``history_lock`` -> ``_sessions_lock`` order [daveey].

Two fork changes met in one merge. The C14 deadlock fix starts a prompt turn without taking a session's
``history_lock`` while holding ``_sessions_lock`` (session.resume holds history_lock, then takes _sessions_lock).
The backend-exit journal made a notice turn's start the commit point for its completions: the exit fence is
checked and the start done under ``_sessions_lock``, and ``start_synthesized_turn`` marked the completions
delivered under ``history_lock`` inside it. Composed naively that is the same ABBA pair again, on every
automatic turn. The commit now marks before ``_sessions_lock`` and checks the fence under it, and the fence
itself is raised under each session's ``history_lock`` first.
"""

from __future__ import annotations

import threading

import pytest

from tests.tui_gateway.test_prompt_start_lock_order import _OrderCheckedLock, _registered_session
from tui_gateway import server
from tui_gateway.exit_job_receipts import EXIT_FENCE, fence_exit_notices
from tui_gateway.session_notifications import (
    COMPLETION_UNSECURED, NOTIF_OFFERED_COMPLETIONS, start_synthesized_turn)

pytestmark = pytest.mark.platforms("posix")

EVENT = {"type": "completion", "session_id": "proc_done", "output": "CI passed"}


@pytest.fixture()
def turn_env(monkeypatch, tmp_path):
    """The model-free turn harness of test_prompt_start_lock_order."""
    for name, value in (("_emit", lambda *a, **k: None), ("_wire_callbacks", lambda sid: None),
                        ("_sync_agent_model_with_config", lambda sid, session: None),
                        ("_session_cwd", lambda session: str(tmp_path)), ("_register_session_cwd", lambda s: None),
                        ("_tts_stream_begin", lambda: None), ("_sync_session_key_after_compress", lambda *a, **k: None),
                        ("_get_usage", lambda agent: {}),
                        ("_submit_row_target_key", lambda session: session["session_key"])):
        monkeypatch.setattr(server, name, value)


@pytest.mark.usefixtures("turn_env")
def test_notice_turn_start_marks_delivery_without_history_lock_under_sessions_lock(request):
    sid = f"noticelock-{request.node.name}"[:60]
    lock, completed = _OrderCheckedLock(), threading.Event()
    session = _registered_session(sid, lock, completed)
    session.update({"_synthesized_turn": True, NOTIF_OFFERED_COMPLETIONS: {"proc_done": dict(EVENT)}})
    try:
        assert server._run_prompt_submit("rid", sid, session, "notice")
        thread = session.get("_run_thread")
        assert thread is not None
        thread.join(10)
        assert not thread.is_alive()
    finally:
        with server._sessions_lock:
            server._sessions.pop(sid, None)
    assert completed.is_set(), "the notice turn must still run"
    assert "proc_done" in session.get("_completion_delivered", set())
    assert lock.under_sessions_lock == [], (
        f"history_lock taken while holding _sessions_lock: {lock.under_sessions_lock}")


def test_fence_between_mark_and_commit_refuses_the_start_and_keeps_the_notice_owed():
    """The mark precedes the fence check now: an exit fencing in between must still see the notice (unsecured,
    which the journal reports), the start must be refused, and the mark must be rolled back."""
    session = {"history_lock": threading.Lock(), "_synthesized_turn": True,
               NOTIF_OFFERED_COMPLETIONS: {"proc_done": dict(EVENT)}}
    starts: list = []
    with start_synthesized_turn(session) as commit:
        assert set(session[COMPLETION_UNSECURED]) == {"proc_done"}  # the journal reports it from here on
        fence_exit_notices([("sid", session)])
        with server._sessions_lock:
            assert commit(lambda: starts.append(1) or object()) is None
    assert starts == [], "a notice turn started after the exit fence"
    assert session[EXIT_FENCE] is True
    assert not session.get("_completion_delivered")
    assert not session.get(COMPLETION_UNSECURED)


def test_exit_fence_takes_history_lock_outside_sessions_lock():
    lock = _OrderCheckedLock()
    session = {"history_lock": lock, "_turn_cancel_requested": False}
    with server._sessions_lock:
        server._sessions["fence-order"] = session
    try:
        fence_exit_notices()
    finally:
        with server._sessions_lock:
            server._sessions.pop("fence-order", None)
    assert session[EXIT_FENCE] is True
    assert lock.acquisitions
    assert lock.under_sessions_lock == [], (
        f"history_lock taken while holding _sessions_lock: {lock.under_sessions_lock}")
