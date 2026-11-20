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


def test_minimum_checks_belong_to_current_park(parked):
    conn, tid, clock = parked
    for hour in (0, 1, 2, 6):
        result(parked, hour)
    clock[0] += 9 * HOUR
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker")
    _park(conn, tid, clock, wake_check=PROBE)
    result(parked, 7)
    result(parked, 14)
    assert len(notices(conn, tid)) == 1
    result(parked, 15)
    assert len(notices(conn, tid)) == 2
    assert notices(conn, tid)[-1].payload["checks"] == 3


def test_check_age_starts_at_first_check_not_park(parked):
    conn, tid, _ = parked
    for hour in (5, 6, 10.9):
        result(parked, hour)
    assert not notices(conn, tid)
    result(parked, 11)
    assert len(notices(conn, tid)) == 1


def test_exact_age_and_backstop_boundaries(parked):
    conn, tid, _ = parked
    # Seven-day backstop minus 24 h: exactly the far-backstop boundary.
    for hour in (138, 140, 144):
        result(parked, hour)
    assert not notices(conn, tid)


def test_exact_age_notifies(parked):
    conn, tid, _ = parked
    for hour in (0, 1, 6):
        result(parked, hour)
    assert len(notices(conn, tid)) == 1


def test_configurable_notice_age(parked, monkeypatch):
    from hermes_cli import config

    conn, tid, _ = parked
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": {"wake_check_notice_hours": 2}})
    for hour in (0, 1, 2):
        result(parked, hour)
    assert len(notices(conn, tid)) == 1


def test_notice_key_is_registered():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["kanban"]["wake_check_notice_hours"] == 6


def test_passive_listener_does_not_notify(parked, monkeypatch):
    from hermes_cli import kanban_passive_intake as passive

    conn, tid, clock = parked
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker")
    _park(conn, tid, clock, wake_check=PROBE,
          awaiting={"kind": "external", "ref": "example vendor", "why": "standing external listener"})
    monkeypatch.setattr(passive, "snapshot", lambda: {tid: PROBE})
    for hour in (0, 1, 7):
        result(parked, hour)
    assert not notices(conn, tid)


def test_continued_park_notifies(parked):
    conn, tid, _ = parked
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET kind='continued' WHERE task_id=? AND kind='scheduled'", (tid,))
    for hour in (0, 1, 7):
        result(parked, hour)
    assert len(notices(conn, tid)) == 1


@pytest.mark.parametrize("value, expected", [(2, 7200), (None, 21600), ("bad", 21600),
                                                 (0, 21600), (-1, 21600), (float("inf"), 21600)])
def test_notice_config_values(monkeypatch, value, expected):
    from hermes_cli import config

    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": {"wake_check_notice_hours": value}})
    assert unproven.notice_seconds() == expected


def test_minimum_three_checks(parked):
    conn, tid, _ = parked
    result(parked, 0)
    result(parked, 7)
    assert not notices(conn, tid)
    result(parked, 8)
    assert len(notices(conn, tid)) == 1


def test_failed_probe_breaks_notice_streak(parked):
    conn, tid, clock = parked
    result(parked, 0)
    result(parked, 3)
    state = wc.get_state(conn, tid)
    wc.apply_result(conn, tid, state["cmd"], 2, "temporary probe failure", generation=state["generation"],
                    now=clock[0] + 4 * HOUR)
    for hour in (7, 8, 12):
        result(parked, hour)
    assert not notices(conn, tid)
    result(parked, 13)
    assert len(notices(conn, tid)) == 1
    assert notices(conn, tid)[0].payload["checks"] == 4


def test_exit_1_with_error_output_breaks_notice_streak(parked):
    conn, tid, clock = parked
    result(parked, 0)
    result(parked, 3)
    state = wc.get_state(conn, tid)
    wc.apply_result(
        conn, tid, state["cmd"], 1, "Traceback (most recent call last):\nValueError: boom",
        generation=state["generation"], now=clock[0] + 4 * HOUR)
    result(parked, 7)
    assert not notices(conn, tid)
    for hour in (8, 12):
        result(parked, hour)
    assert not notices(conn, tid)
    result(parked, 13)
    assert len(notices(conn, tid)) == 1
    assert notices(conn, tid)[0].payload["checks"] == 4


def test_non_one_last_rc_with_zero_fails_breaks_notice_streak(parked):
    conn, tid, _ = parked
    result(parked, 0)
    result(parked, 3)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_wake_checks SET last_rc = ?, fails = 0 WHERE task_id = ?",
                     (wc.UNKNOWN_RC, tid))
    result(parked, 7)
    assert not notices(conn, tid)


def test_spent_park_is_not_used(parked):
    conn, tid, _ = parked
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker")
    assert unproven._latest_park(conn, tid) is None


def test_notice_without_park(kanban_home):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        with kb.write_txn(conn):
            assert not unproven.note_check(conn, tid, PROBE, "", int(time.time()))


def test_notice_is_a_delivered_passive_kind():
    from gateway import kanban_watchers_notifier as gw
    from tui_gateway import session_notifications as tui

    assert KIND in gw.TERMINAL_KINDS and KIND in tui._KANBAN_NOTIFY_KINDS
    assert KIND not in gw._WAKE_KINDS  # never wakes the origin turn
    assert KIND in gw._EVENT_FORMATTERS and KIND in tui._KANBAN_EVENT_FORMATTERS
