"""In-flight custody and held-copy marks compose for events taken off a session's held list [daveey].

``process_registry_awaiting.hold`` puts events a poller took off its ``_notif_held`` list in in-flight custody
(the backend-exit journal reads it) and ``release`` ends it. ``release`` also ends a held-copy mark
(``async_delegation.mark_copy_held``) because ``take_ready`` takes one. The held list keeps its own mark, ended
by ``_notif_unhold``. So ``hold`` must take a mark too: otherwise ``release`` + ``_notif_unhold`` end two marks
for one, and a delegation result still being routed reads as lost to the orphan sweep.
"""

from __future__ import annotations

import collections

import pytest

from tools import async_delegation as ad
from tools import process_registry_awaiting as awaiting


@pytest.fixture
def marks(monkeypatch):
    monkeypatch.setattr(ad, "_held_copies", collections.Counter())
    monkeypatch.setattr(awaiting, "_in_flight", collections.Counter())
    monkeypatch.setattr(awaiting, "_in_flight_events", [])
    return ad._held_copies


def _result(delegation_id: str) -> dict:
    return {"type": "async_delegation", "delegation_id": delegation_id, "owner_task_id": "owner"}


def test_hold_then_release_leaves_the_held_lists_mark(marks):
    evt = _result("d1")
    ad.mark_copy_held("d1")  # the session's held list owns this one (_notif_hold_for)
    awaiting.hold([evt])
    assert marks["d1"] == 2
    assert evt in awaiting._in_flight_events and awaiting._in_flight["owner"] == 1
    awaiting.release([evt])
    assert marks["d1"] == 1, "release ended the held list's mark: the result reads as lost while still owed"
    assert evt not in awaiting._in_flight_events and not awaiting._in_flight
    ad.unmark_copy_held("d1")  # _notif_unhold
    assert not marks


def test_take_ready_then_release_balances_marks_and_custody(marks):
    import queue

    q: queue.Queue = queue.Queue()
    evt = _result("d2")
    q.put(evt)
    taken = awaiting.take_ready(q, 0)
    assert taken == [evt] and marks["d2"] == 1
    assert awaiting.pending_events(q) == [evt]
    awaiting.release(taken)
    assert not marks and awaiting.pending_events(q) == []
