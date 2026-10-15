"""A re-adopted orphan's exit reaches its chat even when the chat is not open yet. [daveey]

After a Desktop restart the new backend re-adopts a chat's ``notify_on_complete`` job
(tools/process_registry_readopt.py). Its exit completion is addressed to that chat's
session_key, but the first poller to dequeue it usually belongs to another chat.
Unowned addressed events are normally dropped; a re-adopted completion is parked
instead, and the owning chat's poller takes it once that chat is open.
"""

from __future__ import annotations

import threading

import pytest

from tools.process_registry import ProcessRegistry
from tui_gateway import server

KEY = "20261014_152627_e39461"
EVENT = {"type": "completion", "session_id": "proc_4fa0a87e0e10", "session_key": KEY, "command": "bash x.sh",
         "exit_code": None, "completion_reason": "unknown", "park_if_unowned": True}


@pytest.fixture
def owners(monkeypatch):
    owned: set[str] = set()
    monkeypatch.setattr(server, "_notification_event_belongs_elsewhere", lambda sid, session, evt: False)
    monkeypatch.setattr(server, "_session_owns_notification_event",
                        lambda sid, session, evt: sid in owned and evt.get("session_key") == KEY)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    return owned


def _session() -> dict:
    return {"history_lock": threading.RLock(), "running": False, "history": [], "agent": None, "profile_home": None}


def test_unowned_readopted_completion_is_parked_not_dropped(owners):
    registry = ProcessRegistry()
    completions: list = []
    assert server._notif_handle_event("other", _session(), dict(EVENT), set(), registry, lambda e: "t", None,
                                      completions) is True
    assert completions == []
    assert registry.completion_queue.empty()
    assert [e["session_id"] for e in registry.take_parked_events(lambda e: True)] == ["proc_4fa0a87e0e10"]


def test_ordinary_unowned_completion_is_still_dropped(owners):
    registry = ProcessRegistry()
    evt = {k: v for k, v in EVENT.items() if k != "park_if_unowned"}
    server._notif_handle_event("other", _session(), evt, set(), registry, lambda e: "t", None, [])
    assert registry.take_parked_events(lambda e: True) == []


def test_owner_poller_takes_the_parked_completion(owners):
    registry = ProcessRegistry()
    registry.park_unowned_event(dict(EVENT))
    handled: list = []
    handle = lambda events, deferred: handled.extend(events)

    assert server._notif_take_parked("other", _session(), registry, handle) == 0
    assert handled == []

    owners.add("owner")
    assert server._notif_take_parked("owner", _session(), registry, handle) == 1
    assert [e["session_key"] for e in handled] == [KEY]
    assert server._notif_take_parked("owner", _session(), registry, handle) == 0, "taken once"


def test_owner_handles_parked_completion_as_a_turn(owners, monkeypatch):
    """End to end through the real handler: the owner batches it into a completion turn."""
    registry = ProcessRegistry()
    registry.park_unowned_event(dict(EVENT))
    owners.add("owner")
    completions: list = []
    handle = lambda events, deferred: [server._notif_handle_event(
        "owner", _session(), e, set(), registry, lambda evt: "wake", deferred, completions) for e in events]
    server._notif_take_parked("owner", _session(), registry, handle)
    assert [(e["session_id"], text) for e, text in completions] == [("proc_4fa0a87e0e10", "wake")]
