"""Clean not-yet probes must eventually ask their worker to reconsider the wait."""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as wc
from tests.hermes_cli import test_kanban_wake_check as helpers

kanban_home = helpers.kanban_home
HOUR = 3600
STALE = "wake_check_stale"


@pytest.fixture
def parked(kanban_home, monkeypatch):
    clock = [int(time.time())]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        assert kb.schedule_task(conn, tid, reason="waiting", wake_check="exit 1")
        yield conn, tid, clock


def result(parked, hours, rc=1):
    conn, tid, clock = parked
    state = wc.get_state(conn, tid)
    now = clock[0] + int(hours * HOUR)
    return wc.apply_result(conn, tid, state["cmd"], rc, "", generation=state["generation"], now=now)


def repark(parked, cmd="exit 1"):
    conn, tid, _ = parked
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    assert kb.schedule_task(conn, tid, reason="waiting", wake_check=cmd)


def test_always_not_yet_wakes_once_at_48_hours(parked):
    conn, tid, _ = parked
    for hour in (0, 12, 24, 47):
        assert result(parked, hour) is None
    assert result(parked, 48) == STALE
    assert kb.get_task(conn, tid).status == "ready"
    assert len(helpers._events(conn, tid, STALE)) == 1
    text = "\n".join(c.body for c in kb.list_comments(conn, tid))
    assert "wake_check has not passed in 48 h (5 checks since " in text
    assert " PT): check that its condition can occur" in text
    assert "Re-parking on the identical probe keeps this episode." in text
    assert "wake_check has not passed" in kb.build_worker_context(conn, tid)


def test_pass_inside_window_resets_episode(parked):
    conn, tid, _ = parked
    result(parked, 0)
    assert result(parked, 24, 0) == "condition_met"
    repark(parked)
    result(parked, 25)
    assert result(parked, 48) is None
    assert not helpers._events(conn, tid, STALE)


def test_same_probe_repark_waits_another_48_hours(parked):
    conn, tid, _ = parked
    result(parked, 0)
    assert result(parked, 48) == STALE
    repark(parked)
    for hour in (49, 72, 95):
        assert result(parked, hour) is None
    assert len(helpers._events(conn, tid, STALE)) == 1
    assert result(parked, 96) == STALE
    assert len(helpers._events(conn, tid, STALE)) == 2


def test_changed_probe_starts_new_episode(parked):
    conn, tid, _ = parked
    result(parked, 0)
    with kb.write_txn(conn):
        wc.set_check(conn, tid, "test -f new-flag", 120)
    result(parked, 47)
    assert result(parked, 48) is None
    assert result(parked, 95) == STALE


def test_exit_two_does_not_advance_episode(parked):
    conn, tid, clock = parked
    result(parked, 0)
    assert result(parked, 49, 2) is None
    assert not helpers._events(conn, tid, STALE)
    state = wc.get_state(conn, tid)
    assert state["episode_first_at"] == clock[0]
    assert state["episode_checks"] == 1
    assert state["fails"] == 1
    assert result(parked, 50) == STALE
    assert helpers._events(conn, tid, STALE)[0].payload["checks"] == 2


@pytest.mark.parametrize("status", ["blocked", "triage", "done", "archived"])
def test_leaving_wait_cycle_resets_even_without_a_dispatch_tick(parked, status):
    conn, tid, clock = parked
    result(parked, 0)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
        conn.execute("UPDATE tasks SET status = 'scheduled' WHERE id = ?", (tid,))
        wc.set_check(conn, tid, "exit 1", 120)
    assert result(parked, 48) is None
    assert wc.get_state(conn, tid)["episode_first_at"] == clock[0] + 48 * HOUR


@pytest.mark.parametrize("rc", [2, 124, 255, "timeout", None])
def test_errors_do_not_start_an_episode(parked, rc):
    conn, tid, _ = parked
    assert result(parked, 0, rc) is None
    state = wc.get_state(conn, tid)
    assert state["episode_first_at"] is None
    assert state["fails"] == 1


def test_clock_wake_repark_preserves_episode(parked):
    conn, tid, _ = parked
    result(parked, 0)
    from hermes_cli.kanban_continuation import wake_due_continuations
    assert wake_due_continuations(conn, now=parked[2][0] + 25 * HOUR) == [tid]
    repark(parked)
    assert result(parked, 48) == STALE


def test_cleared_probe_resets_episode(parked):
    conn, tid, _ = parked
    result(parked, 0)
    with kb.write_txn(conn):
        wc.set_check(conn, tid, None, 120)
        wc.set_check(conn, tid, "exit 1", 120)
    assert result(parked, 48) is None


def test_old_generation_cannot_count_or_reset(parked):
    conn, tid, _ = parked
    result(parked, 0)
    old = wc.get_state(conn, tid)
    with kb.write_txn(conn):
        wc.set_check(conn, tid, "exit 1", 120)
    for rc in (0, 1):
        assert wc.apply_result(conn, tid, "exit 1", rc, "", generation=old["generation"],
                               now=parked[2][0] + 48 * HOUR) is None
    assert wc.get_state(conn, tid)["episode_checks"] == 1
    assert result(parked, 48) == STALE


def test_configured_interval_and_live_state_rendering(parked, kanban_home):
    from hermes_cli.config import atomic_config_write

    atomic_config_write(kanban_home / "config.yaml", {"kanban": {"wake_check_stale_hours": 2}})
    conn, tid, _ = parked
    result(parked, 0)
    assert result(parked, 1) is None
    state = wc.load_states(conn, [tid])[tid]
    assert state["episode_checks"] == 2
    assert "not passed since" in wc.describe_state(state)
    assert "(2 checks)" in wc.describe_state(state)
    assert result(parked, 2) == STALE


def test_initial_pass_then_failed_confirmation_resets(parked):
    conn, tid, clock = parked
    result(parked, 0)
    state = wc.get_state(conn, tid)
    assert wc.apply_result(conn, tid, "exit 1", 1, "", generation=state["generation"],
                           now=clock[0] + 48 * HOUR, diagnostics={"first_rc": 0}) is None
    assert wc.get_state(conn, tid)["episode_checks"] == 1
    assert wc.get_state(conn, tid)["episode_first_at"] == clock[0] + 48 * HOUR


def test_dispatcher_runs_and_persists_episode_across_connections(parked, monkeypatch):
    conn, tid, clock = parked
    monkeypatch.setattr(wc, "_run_probe", lambda *args: (1, "waiting"))
    monkeypatch.setattr(wc, "finished_producer", lambda *args: None)
    from hermes_cli import kanban_dead_ancestry
    monkeypatch.setattr(kanban_dead_ancestry, "detect", lambda *args: None)
    for hour in (0, 24, 48):
        submitted = wc.run_due_wake_checks(conn, now=clock[0] + hour * HOUR, wait=True)
        assert submitted["submitted"] == [tid]
    assert kb.get_task(conn, tid).status == "ready"
    assert len(helpers._events(conn, tid, STALE)) == 1
    assert helpers._events(conn, tid, STALE)[0].payload["checks"] == 3


def test_readonly_legacy_probe_state_remains_visible(parked):
    conn, tid, _ = parked
    conn.execute("DROP TABLE task_wake_episodes")
    state = wc.get_state(conn, tid)
    assert state["cmd"] == "exit 1"
    assert state["episode_first_at"] is None
    assert wc.load_states(conn)[tid] == state
    assert wc.load_states(conn, [tid])[tid] == state
    assert wc.load_states(conn, []) == {}
    assert wc.get_state(conn, "absent") is None


def test_eviction_deletes_episode(parked):
    conn, tid, _ = parked
    result(parked, 0)
    with kb.write_txn(conn):
        conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    assert conn.execute("SELECT * FROM task_wake_episodes").fetchall() == []


@pytest.mark.parametrize("value", [0, -1, "invalid", float("nan"), float("inf"), None])
def test_invalid_config_uses_default(monkeypatch, value):
    from hermes_cli import config
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": {"wake_check_stale_hours": value}})
    assert wc.episode.stale_seconds() == 48 * HOUR


def test_config_backend_outage_keeps_default(monkeypatch):
    from hermes_cli import config
    from hermes_cli.config_backend import ConfigBackendUnavailable

    def unavailable():
        raise ConfigBackendUnavailable("unavailable")

    monkeypatch.setattr(config, "load_config_readonly", unavailable)
    assert wc.episode.stale_seconds() == 48 * HOUR
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": None})
    assert wc.episode.stale_seconds() == 48 * HOUR


def test_stale_wake_resets_ready_diagnostics_clock(parked):
    from hermes_cli.kanban_diagnostics import _last_ready_entry_ts
    conn, tid, clock = parked
    result(parked, 0)
    clock[0] += 48 * HOUR
    assert result(parked, 0) == STALE
    assert _last_ready_entry_ts(kb.list_events(conn, tid)) == clock[0]


def test_pool_uses_dispatcher_interval_without_reading_config_in_transaction(parked, monkeypatch):
    import threading
    conn, tid, clock = parked
    main_thread = threading.get_ident()

    def interval():
        assert threading.get_ident() == main_thread
        assert not conn.in_transaction
        return 2 * HOUR

    monkeypatch.setattr(wc.episode, "stale_seconds", interval)
    monkeypatch.setattr(wc, "_run_probe", lambda *args: (1, ""))
    monkeypatch.setattr(wc, "finished_producer", lambda *args: None)
    from hermes_cli import kanban_dead_ancestry
    monkeypatch.setattr(kanban_dead_ancestry, "detect", lambda *args: None)
    for hour in (0, 2):
        assert wc.run_due_wake_checks(conn, now=clock[0] + hour * HOUR, wait=True)["submitted"] == [tid]
    assert len(helpers._events(conn, tid, STALE)) == 1
