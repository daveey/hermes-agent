"""A synthesized turn refused because another device holds the chat is not retried in a loop.

2026-10-28, David-MBP: a background-process completion addressed to chat 20261023_204423_b85a94 sat in
the MacBook Desktop backend while the mini's Desktop (an SSH backend on the same Mac) held the chat's
active-session lease. The session's notification poller claimed the idle session, ran the completion
turn, ``_admit_prompt_turn`` refused it SESSION_NOT_OWNED (message.start + an error message.complete),
``_notif_retry_completions`` put the event straight back on the queue after 0.25 s, and the poller
dequeued it again: 139 "Hermes hit an internal error ... Your message was not answered" cards in 110 s,
one every ~0.78 s, until hermes-sync's drain closed admission.

These tests drive the REAL poller thread, dispatch, ``_run_prompt_submit`` and ``_admit_prompt_turn``
against a real active-session registry entry held by another writer, and assert bounded attempts and a
single notice whose copy says where the chat is open, not "internal error".
"""

from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

import pytest

from tui_gateway import server

KEY = "20261023_204423_b85a94"


def _completion() -> dict:
    return {"type": "completion", "session_id": "proc_refused", "session_key": KEY, "command": "sleep 1",
            "exit_code": 0, "output": "done"}


@pytest.fixture
def held_elsewhere(monkeypatch):
    """A real registry lease on KEY held by another writer (another device's backend)."""
    from hermes_cli.active_sessions import try_acquire_active_session

    lease, refusal = try_acquire_active_session(
        session_id=KEY, surface="desktop", config={},
        metadata={"live_session_id": "mini-ssh-backend"})
    assert refusal is None and lease is not None
    yield lease
    lease.release()


@pytest.fixture
def poller_env(monkeypatch):
    from tools.process_registry import process_registry

    q: queue.Queue = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    monkeypatch.setattr(process_registry, "restore_completions", lambda: 0)
    process_registry._completion_consumed.discard("proc_refused")
    frames: list = []
    lock = threading.Lock()

    def _emit(event, sid, payload=None):
        with lock:
            frames.append((event, payload))

    monkeypatch.setattr(server, "_emit", _emit)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    # No state.db writer lease in this sandbox: the registry lease below is the only owner signal.
    monkeypatch.setattr(server, "_foreign_turn_owner", lambda session: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda session: True)
    # Fork: upstream retired the bot-mailbox claim from the notification poller (c8ae25cd112, kept by
    # the merge), so there is no _poll_bot_live_delivery_guarded pass left to stub out.
    for name in ("_maybe_fire_tui_loop_tick",
                 "_maybe_fire_tui_heartbeat_tick", "_notif_poll_kanban", "_notif_take_parked"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr("tools.async_delegation.maybe_sweep_orphaned_completions", lambda q: None)
    monkeypatch.setattr("tools.async_delegation.claim_event_delivery", lambda evt, consumer: "claim")
    monkeypatch.setattr("tools.async_delegation.release_event_delivery", lambda evt, claim: None)
    completed: list = []
    monkeypatch.setattr("tools.async_delegation.complete_event_delivery",
                        lambda evt, claim: completed.append(evt))
    turns: list = []
    monkeypatch.setattr(server, "_notif_resolve_event_key", lambda evt_key, session=None: evt_key)
    session = {"session_key": KEY, "history": [], "history_version": 0, "history_lock": threading.RLock(),
               "running": False, "agent": SimpleNamespace(run_conversation=lambda *a, **k: turns.append(a)),
               "attached_images": [], "cols": 80, "source": "desktop", "inflight_turn": None}
    sid = "56b01ba7"
    with server._sessions_lock:
        saved = dict(server._sessions)
        server._sessions.clear()
        server._sessions[sid] = session
    try:
        yield SimpleNamespace(queue=q, frames=frames, lock=lock, session=session, sid=sid, turns=turns,
                              completed=completed)
    finally:
        with server._sessions_lock:
            server._sessions.clear()
            server._sessions.update(saved)
        process_registry._completion_consumed.discard("proc_refused")


def _run_poller(env, seconds: float) -> None:
    stop = threading.Event()
    worker = threading.Thread(target=server._notification_poller_scoped_loop,
                              args=(stop, env.sid, env.session), daemon=True)
    worker.start()
    time.sleep(seconds)
    stop.set()
    env.session["_finalized"] = True
    worker.join(timeout=10)
    assert not worker.is_alive()


def _counts(env):
    with env.lock:
        starts = [p for e, p in env.frames if e == "message.start"]
        errors = [p for e, p in env.frames if e == "message.complete" and (p or {}).get("status") == "error"]
    return starts, errors


def test_owner_refusal_is_not_retried_in_a_loop_and_shows_one_notice(poller_env, held_elsewhere):
    poller_env.queue.put(_completion())

    _run_poller(poller_env, 4.0)

    starts, errors = _counts(poller_env)
    # Before the fix: a message.start + error message.complete pair every ~0.3-0.8 s for as long as the
    # other device held the chat.
    assert len(errors) == 1, f"one notice, not a loop: {len(errors)} refusals in 4 s"
    assert len(starts) <= 1
    notice = errors[0]
    assert notice["error_surface"]["code"] == "SESSION_NOT_OWNED"
    assert "internal error" not in notice["text"]
    assert "open in another Hermes window or device" in notice["text"]
    assert poller_env.turns == [], "the refused turn must never reach the model"
    # The completion is neither lost nor marked delivered: it waits for the chat to come free.
    assert poller_env.completed == []
    assert poller_env.session["running"] is False


def test_refused_completion_is_delivered_once_the_other_device_lets_go(poller_env, held_elsewhere, monkeypatch):
    """Backoff, not a drop: after the holder releases, the next attempt runs the turn."""
    monkeypatch.setattr(server, "_OWNER_REFUSAL_BACKOFF_S", 0.5, raising=False)
    delivered: list = []
    real_submit = server._run_prompt_submit

    def _submit(rid, sid, session, text, **kwargs):
        if held_elsewhere.released:
            delivered.append(text)
            with session["history_lock"]:
                session["running"] = False
            return True
        return real_submit(rid, sid, session, text, **kwargs)

    monkeypatch.setattr(server, "_run_prompt_submit", _submit)
    poller_env.queue.put(_completion())
    stop = threading.Event()
    worker = threading.Thread(target=server._notification_poller_scoped_loop,
                              args=(stop, poller_env.sid, poller_env.session), daemon=True)
    worker.start()
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline and not _counts(poller_env)[1]:
        time.sleep(0.05)
    held_elsewhere.release()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not delivered:
        time.sleep(0.05)
    stop.set()
    poller_env.session["_finalized"] = True
    worker.join(timeout=10)

    assert len(_counts(poller_env)[1]) == 1
    assert len(delivered) == 1, "the held completion must be delivered after the release"


def test_a_user_prompt_refusal_still_reports_every_time(poller_env, held_elsewhere):
    """Control: the backoff covers synthesized turns only. A prompt the user typed gets its card."""
    for _ in range(2):
        assert server._run_prompt_submit("rid", poller_env.sid, poller_env.session, "hello") is False
    assert len(_counts(poller_env)[1]) == 2
