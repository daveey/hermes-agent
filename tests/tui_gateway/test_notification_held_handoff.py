"""Owner-addressed notifications wait with their owner, not on the shared completion queue.

Every live session's poller drains one process-wide completion queue. Before this, an event whose owner was
mid-turn went back on that queue: every other poller dequeued it, resolved its compression lineage against
state.db and re-queued it, ten times a second each, for the whole turn. On a 22-chat Desktop backend that loop
held the GIL hot enough to stall the event loop 5-13 s and time out every Desktop request (2026-10-27).
"""

from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

import pytest

from tui_gateway import server


def _sess(key: str, *, running: bool = False, poller: bool = True) -> dict:
    s = {"session_key": key, "history": [], "history_lock": threading.RLock(), "running": running,
         "agent": SimpleNamespace()}
    if poller:
        s["_notif_held"] = []
    return s


def _completion(key: str, proc: str = "proc_held") -> dict:
    return {"type": "completion", "session_id": proc, "session_key": key, "command": "echo hi",
            "exit_code": 0, "output": "hi"}


@pytest.fixture
def env(monkeypatch):
    """Isolated sessions registry + completion queue; lineage lookups counted (identity resolve)."""
    from tools.process_registry import process_registry

    q: queue.Queue = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    emitted: list = []
    monkeypatch.setattr(server, "_emit", lambda *a, **k: emitted.append(a))
    resolves: list = []

    def _resolve(evt_key, session=None):
        resolves.append(evt_key)
        return evt_key

    monkeypatch.setattr(server, "_notif_resolve_event_key", _resolve)
    delivered: list = []
    monkeypatch.setattr(server, "_run_prompt_submit",
                        lambda _rid, sid, session, text, **_k: delivered.append((sid, text)) or True)
    with server._sessions_lock:
        saved = dict(server._sessions)
        server._sessions.clear()
    try:
        yield SimpleNamespace(queue=q, registry=process_registry, emitted=emitted, resolves=resolves,
                              delivered=delivered)
    finally:
        with server._sessions_lock:
            server._sessions.clear()
            server._sessions.update(saved)
        for proc in ("proc_held", "proc_busy", "proc_drain", "proc_load"):
            process_registry._completion_consumed.discard(proc)


def _add(**sessions):
    with server._sessions_lock:
        server._sessions.update(sessions)


def _handle(sid, session, evt, env, deferred=None):
    return server._notif_handle_event(sid, session, evt, set(), env.registry, lambda e: "t", deferred)


def test_foreign_event_goes_to_the_owners_poller_not_the_shared_queue(env):
    owner, other = _sess("key-a", running=True), _sess("key-b")
    _add(sid_a=owner, sid_b=other)
    evt = _completion("key-a")

    started = time.monotonic()
    assert _handle("sid_b", other, evt, env) is True

    assert env.queue.empty()
    assert owner["_notif_held"] == [evt]
    assert time.monotonic() - started < 0.09  # no requeue back-off sleep


def test_foreign_event_whose_owner_has_no_poller_still_uses_the_shared_queue(env):
    owner, other = _sess("key-a", poller=False), _sess("key-b")
    _add(sid_a=owner, sid_b=other)
    evt = _completion("key-a")

    assert _handle("sid_b", other, evt, env) is True

    assert env.queue.qsize() == 1 and env.queue.get_nowait() is evt


@pytest.mark.parametrize("evt_type", ["completion", "async_delegation"])
def test_a_busy_owner_holds_its_own_event(env, evt_type):
    owner = _sess("key-a", running=True)
    _add(sid_a=owner)
    evt = {**_completion("key-a", "proc_busy"), "type": evt_type, "delegation_id": "deleg-held"}

    if evt_type == "completion":  # completions take the batch path
        server._notif_handle_ready("sid_a", owner, [evt], set(), env.registry, lambda e: "t", None)
    else:
        assert _handle("sid_a", owner, evt, env) is True

    assert env.queue.empty()
    assert owner["_notif_held"] == [evt]


def test_a_busy_session_requeues_an_ownerless_event_for_any_idle_poller(env):
    busy = _sess("key-a", running=True)
    _add(sid_a=busy)
    evt = {"type": "watch_match", "session_id": "proc_x", "command": "c", "pattern": "p", "output": "o"}

    assert _handle("sid_a", busy, evt, env) is True

    assert busy["_notif_held"] == []
    assert env.queue.get_nowait() is evt


class _StopAfter:
    def __init__(self, checks: int):
        self._left = checks

    def is_set(self):
        self._left -= 1
        return self._left < 0


def test_held_event_is_delivered_once_the_owner_goes_idle(env):
    owner = _sess("key-a")
    owner["_notif_held"].append(_completion("key-a"))
    _add(sid_a=owner)

    server._notification_poller_loop(_StopAfter(1), "sid_a", owner)

    assert [sid for sid, _ in env.delivered] == ["sid_a"]
    assert "proc_held" in env.delivered[0][1]
    assert env.queue.empty()


def test_shutdown_drain_returns_held_events_to_the_shared_queue(env):
    owner = _sess("key-a", running=True)
    evt = _completion("key-a", "proc_drain")
    owner["_notif_held"].append(evt)
    _add(sid_a=owner)
    stop = threading.Event()
    stop.set()

    server._notification_poller_loop(stop, "sid_a", owner)

    assert "_notif_held" not in owner
    assert env.queue.get_nowait() is evt
    # A handoff that loses the race with the drain falls back to the shared queue instead of being stranded.
    assert server._notif_hold_for(owner, _completion("key-a"), env.queue) is False


def test_busy_owner_among_many_pollers_does_not_bounce_its_event(env):
    """The live failure: one busy owner, many idle pollers. Each foreign dequeue used to resolve lineage and
    re-queue, so lookups grew with pollers x time; now the event is handed over once and waits."""
    owner = _sess("key-owner", running=True)
    others = {f"sid_{i}": _sess(f"key-{i}") for i in range(6)}
    _add(sid_owner=owner, **others)
    stops, threads = [], []
    for sid, s in [("sid_owner", owner), *others.items()]:
        stop = threading.Event()
        t = threading.Thread(target=server._notification_poller_loop, args=(stop, sid, s), daemon=True)
        stops.append(stop)
        threads.append(t)
        t.start()
    try:
        time.sleep(0.3)  # every poller has created its held list
        env.queue.put(_completion("key-owner", "proc_load"))
        time.sleep(1.5)

        assert len(env.resolves) <= 3, f"event bounced: {len(env.resolves)} lineage lookups"
        assert env.queue.empty()
        assert [e["session_id"] for e in owner["_notif_held"]] == ["proc_load"]
        assert env.delivered == []

        owner["running"] = False  # the turn ends: the owner's own poller delivers it
        deadline = time.monotonic() + 3
        while not env.delivered and time.monotonic() < deadline:
            time.sleep(0.05)
        assert [sid for sid, _ in env.delivered] == ["sid_owner"]
    finally:
        for stop in stops:
            stop.set()
        for t in threads:
            t.join(timeout=5)
