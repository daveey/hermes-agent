"""A probe that keeps saying "not yet" under a far backstop notifies its chat once per park. [daveey]"""
from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as wc
from hermes_cli import kanban_wake_unproven as unproven
from tests.hermes_cli import test_kanban_wake_check as helpers

kanban_home = helpers.kanban_home
HOUR = 3600
KIND = unproven.NOTICE_KIND
PROBE = "python3 run6_ready.py"


def _park(conn, tid, clock, *, days=7, **kw):
    until = datetime.fromtimestamp(clock[0] + days * 24 * HOUR, UTC).isoformat()
    reason = f"waiting SCHEDULED_UNTIL={until}" if days else "waiting"
    assert kb.schedule_task(conn, tid, reason=reason, **kw)


@pytest.fixture
def parked(kanban_home, monkeypatch):
    clock = [int(time.time())]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        _park(conn, tid, clock, wake_check=PROBE)
        yield conn, tid, clock


def result(parked, hours, out="pids still alive"):
    conn, tid, clock = parked
    state = wc.get_state(conn, tid)
    return wc.apply_result(conn, tid, state["cmd"], 1, out, generation=state["generation"],
                           now=clock[0] + int(hours * HOUR))


def notices(conn, tid):
    return helpers._events(conn, tid, KIND)


def test_not_ready_past_age_with_far_backstop_notifies_exactly_once(parked):
    conn, tid, _ = parked
    for hour in (0, 2, 5):
        result(parked, hour)
    assert not notices(conn, tid)
    for hour in (6, 7, 12, 20):
        result(parked, hour)
    found = notices(conn, tid)
    assert len(found) == 1
    assert found[0].payload["wake_check"] == PROBE
    assert found[0].payload["output"] == "pids still alive"
    assert PROBE in unproven.notice_text(found[0].payload)
    assert kb.get_task(conn, tid).status == "scheduled"  # detection only: nobody is woken


def test_not_ready_under_the_age_does_not_notify(parked):
    conn, tid, _ = parked
    for hour in (0, 1, 2, 3, 4, 5.9):
        result(parked, hour)
    assert not notices(conn, tid)


def test_backstop_within_24_hours_does_not_notify(kanban_home, monkeypatch):
    clock = [int(time.time())]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        _park(conn, tid, clock, days=0, wake_check=PROBE)  # default backstop is exactly 24 h
        for hour in (0, 3, 7, 12):
            result((conn, tid, clock), hour)
        assert not notices(conn, tid)


def test_repark_starts_a_new_episode(parked):
    conn, tid, clock = parked
    for hour in (0, 3, 6, 8):
        result(parked, hour)
    assert len(notices(conn, tid)) == 1
    clock[0] += 9 * HOUR
    with kb.write_txn(conn):  # a wake, as the dispatcher does it
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    _park(conn, tid, clock, wake_check=PROBE)
    for hour in (0, 2, 5):
        result(parked, hour)
    assert len(notices(conn, tid)) == 1  # the new episode has not aged yet
    for hour in (6, 7, 10):
        result(parked, hour)
    assert len(notices(conn, tid)) == 2


def test_clock_only_wait_never_notifies(kanban_home, monkeypatch):
    clock = [int(time.time())]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        _park(conn, tid, clock, clock_only=True)
        assert wc.get_state(conn, tid) is None  # no probe runs, so nothing can count a check
        assert unproven._latest_park(conn, tid) is None  # the park is marked clock-only
        clock[0] += 12 * HOUR
        assert unproven.maybe_notice(conn, tid, PROBE, "", now=clock[0], first_at=clock[0] - 12 * HOUR,
                                     checks=5) is False
        assert not notices(conn, tid)


def test_notice_is_a_delivered_passive_kind():
    from gateway import kanban_watchers_notifier as gw
    from tui_gateway import session_notifications as tui

    assert KIND in gw.TERMINAL_KINDS and KIND in tui._KANBAN_NOTIFY_KINDS
    assert KIND not in gw._WAKE_KINDS  # never wakes the origin turn
    assert KIND in gw._EVENT_FORMATTERS and KIND in tui._KANBAN_EVENT_FORMATTERS
