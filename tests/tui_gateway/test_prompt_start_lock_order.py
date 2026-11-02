"""Starting a prompt turn never takes a session's ``history_lock`` while holding ``_sessions_lock`` [daveey].

session.resume / session.activate rebind a live transport under ``history_lock`` and then take
``_sessions_lock`` (``_rebind_live_transport`` -> ``_cancel_ws_orphan_reap``). ``_run_admitted_prompt``
used to start the turn worker inside its ``_sessions_lock`` block, and ``start_work`` takes that same
session's ``history_lock`` to retain the turn's admission. A resume of the session racing its own
prompt.submit then deadlocked the two threads, and every later create/close/resume on the backend
queued behind the leaked ``_sessions_lock`` (C14 seed 11: resume hung 60 s, seeds 23/37 could not
even create a session). The py-spy dump of the wedged backend showed exactly that pair.
"""

from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import server

pytestmark = pytest.mark.platforms("posix")


class _OrderCheckedLock:
    """A session ``history_lock`` that records every acquisition made while ``_sessions_lock`` is held."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.under_sessions_lock: list[str] = []
        self.acquisitions = 0
        self.watched_thread: str | None = None
        self.contended = threading.Event()  # set when ``watched_thread`` waits on this lock

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        self.acquisitions += 1
        if getattr(server._sessions_lock, "_is_owned")():
            self.under_sessions_lock.append(threading.current_thread().name)
        if threading.current_thread().name == self.watched_thread and self._lock.locked():
            self.contended.set()
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class _Db:
    def append_message(self, **kw):
        return 1


@pytest.fixture()
def turn_env(monkeypatch, tmp_path):
    frames: list[tuple] = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None, *a, **k: frames.append((event, payload)))
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    monkeypatch.setattr(server, "_submit_row_target_key", lambda session: session["session_key"])
    return frames


def _registered_session(sid: str, lock: _OrderCheckedLock, completed: threading.Event) -> dict:
    def run_conversation(*a, **k):
        completed.set()
        return {"final_response": "ok", "messages": [], "interrupted": False, "completed": True}

    agent = types.SimpleNamespace(session_id=f"agent-{sid}", run_conversation=run_conversation,
                                  clear_interrupt=lambda: None, interrupt=lambda *a, **k: True, _session_db=_Db())
    session = {
        "agent": agent, "agent_ready": threading.Event(), "agent_error": None, "history": [],
        "history_lock": lock, "running": True, "session_key": sid, "history_version": 0,
        "attached_images": [], "image_counter": 0, "cols": 80, "slash_worker": None, "show_reasoning": False,
        "tool_progress_mode": "all", "inflight_turn": None,
    }
    session["agent_ready"].set()
    with server._sessions_lock:
        server._sessions[sid] = session
    return session


def test_registered_turn_start_takes_history_lock_outside_sessions_lock(turn_env, request):
    sid = f"lockorder-{request.node.name}"[:60]
    lock, completed = _OrderCheckedLock(), threading.Event()
    session = _registered_session(sid, lock, completed)
    try:
        assert server._run_prompt_submit("rid", sid, session, "go")
        thread = session.get("_run_thread")
        assert thread is not None
        thread.join(10)
        assert not thread.is_alive()
    finally:
        with server._sessions_lock:
            server._sessions.pop(sid, None)
    assert completed.is_set(), "the turn must still run"
    assert lock.acquisitions, "the start path must still retain admission under history_lock"
    assert lock.under_sessions_lock == [], (
        "history_lock taken while holding _sessions_lock (resume/activate take them in the opposite order): "
        f"{lock.under_sessions_lock}")


def test_resume_rebind_racing_turn_start_does_not_deadlock(turn_env, request):
    """The C14 interleaving, forced: a resume holds the session's history_lock and is about to cancel the
    orphan reap (``_sessions_lock``) while prompt.submit's worker starts the turn."""
    sid = f"lockrace-{request.node.name}"[:60]
    lock, completed = _OrderCheckedLock(), threading.Event()
    session = _registered_session(sid, lock, completed)
    lock.watched_thread = "prompt-submit-under-test"
    contended = lock.contended
    result: dict = {}
    submit = threading.Thread(
        target=lambda: result.setdefault("ok", server._run_prompt_submit("rid", sid, session, "go")),
        name="prompt-submit-under-test", daemon=True)
    stalls = []
    try:
        with lock:
            submit.start()
        # At EVERY point where prompt.submit waits for history_lock, a resume holding it must still be able
        # to take _sessions_lock; then let prompt.submit through that one acquisition and repeat.
        while submit.is_alive():
            with lock:  # session.resume: _live_session_payload holds history_lock ...
                if not contended.wait(0.5):
                    continue
                contended.clear()  # cleared while prompt.submit is blocked on us, so no wait is missed
                # ... then _rebind_live_transport -> _cancel_ws_orphan_reap takes _sessions_lock.
                got = server._sessions_lock.acquire(timeout=3)
                if got:
                    server._sessions_lock.release()
                else:
                    stalls.append("turn start held _sessions_lock while waiting for history_lock")
        submit.join(10)
        assert stalls == [], f"deadlock: {stalls}"
        thread = session.get("_run_thread")
        if thread is not None:
            thread.join(10)
    finally:
        with server._sessions_lock:
            server._sessions.pop(sid, None)
    assert result.get("ok") is True
    assert completed.is_set()


@pytest.mark.parametrize("claimed", [True, False], ids=["submit-claimed-admission", "no-prior-admission"])
def test_turn_refused_for_a_closing_session_gives_back_the_prepared_admission(
        turn_env, request, tmp_path, monkeypatch, claimed):
    """Retaining the admission before the ``_closing`` check must not leak it when the check refuses the
    turn: no worker runs and no update-admission reservation outlives the refused turn (as before the
    admission was retained ahead of the check)."""
    from hermes_cli import backend_retirement
    from tui_gateway import turn_admission as ta

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    sid = f"lockclose-{request.node.name}"[:60]
    completed = threading.Event()
    session = _registered_session(sid, _OrderCheckedLock(), completed)
    session.update(profile_home=str(tmp_path), running=False)
    try:
        if claimed:  # prompt.submit's claim: the session owns one admission until terminal settlement
            with ta.admission(session) as accepted:
                assert accepted
                session["running"] = True
        session["_closing"] = True
        assert server._run_prompt_submit("rid", sid, session, "go") is False
        assert not completed.is_set()
        assert "_run_thread" not in session
        assert fence.active_count() == 0
        assert session.get("_turn_admission") is None
        assert session["running"] is False
    finally:
        with session["history_lock"]:
            ta.settle(session)
        with server._sessions_lock:
            server._sessions.pop(sid, None)
    assert fence.active_count() == 0
