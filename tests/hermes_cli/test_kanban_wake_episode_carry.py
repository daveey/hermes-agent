"""Episode payload transport, including remapped ids and coalesced status updates."""
import sqlite3
import uuid

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_wake_check as wc
from tests.hermes_cli import test_kanban_wake_episode as base

kanban_home = base.kanban_home
parked = base.parked
result = base.result
repark = base.repark
HOUR = base.HOUR


def replica(conn):
    other = sqlite3.connect(":memory:", isolation_level=None)
    other.row_factory = sqlite3.Row
    conn.backup(other)
    other.execute("UPDATE wake_episode_identity SET source = ? WHERE id = 1", (str(uuid.uuid4()),))
    return other


def import_events(source, target, tid):
    # Transport immutable payloads; intentionally remap local integer ids.
    known = {tuple(row) for row in target.execute(
        "SELECT kind, payload, created_at FROM task_events WHERE task_id = ?", (tid,))}
    for row in source.execute("SELECT kind, payload, created_at FROM task_events WHERE task_id = ?", (tid,)):
        if tuple(row) in known:
            continue
        target.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                       (tid, *row))


def test_stale_wake_carries_age_count_and_cooldown(parked):
    conn, tid, clock = parked
    other = replica(conn)
    try:
        result(parked, 0)
        assert result(parked, 48) == base.STALE
        import_events(conn, other, tid)
        other.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        moved = (other, tid, clock)
        repark(moved)
        assert result(moved, 49) is None
        state = wc.get_state(other, tid)
        assert state["episode_first_at"] == clock[0]
        assert state["episode_checks"] == 3
        assert state["episode_last_stale_at"] == clock[0] + 48 * HOUR
        assert result(moved, 96) == base.STALE
    finally:
        other.close()


def test_coalesced_blocked_transition_resets(parked):
    conn, tid, clock = parked
    result(parked, 0)
    other = replica(conn)
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
            kb._append_event(conn, tid, "blocked", {"reason": "different work"})
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        repark(parked)
        import_events(conn, other, tid)
        assert kb.get_task(other, tid).status == "scheduled"
        assert result((other, tid, clock), 48) is None
        assert wc.get_state(other, tid)["episode_first_at"] == clock[0] + 48 * HOUR
    finally:
        other.close()


def test_unconfirmed_success_reset_is_portable(parked):
    conn, tid, clock = parked
    result(parked, 0)
    other = replica(conn)
    try:
        state = wc.get_state(conn, tid)
        wc.apply_result(conn, tid, "exit 1", 2, "", generation=state["generation"],
                        now=clock[0] + 24 * HOUR, diagnostics={"first_rc": 0})
        import_events(conn, other, tid)
        assert result((other, tid, clock), 48) is None
        assert wc.get_state(other, tid)["episode_first_at"] == clock[0] + 48 * HOUR
    finally:
        other.close()


def test_clock_wake_carries_exact_count(parked):
    from hermes_cli.kanban_continuation import wake_due_continuations
    conn, tid, clock = parked
    other = replica(conn)
    try:
        result(parked, 0)
        result(parked, 12)
        assert wake_due_continuations(conn, now=clock[0] + 25 * HOUR) == [tid]
        import_events(conn, other, tid)
        other.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        repark((other, tid, clock))
        assert result((other, tid, clock), 48) == base.STALE
        assert base.helpers._events(other, tid, base.STALE)[0].payload["checks"] == 3
    finally:
        other.close()


def test_clean_checks_do_not_append_one_event_each(parked):
    conn, tid, _ = parked
    result(parked, 0)
    count = conn.execute("SELECT count(*) FROM task_events").fetchone()[0]
    for hour in (1, 2, 3, 4):
        result(parked, hour)
    assert conn.execute("SELECT count(*) FROM task_events").fetchone()[0] == count
    assert wc.get_state(conn, tid)["episode_checks"] == 5
