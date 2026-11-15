"""G2e (t_01a1dfed10d223e3): cut the per-tick reclaim cost without changing what a tick decides. [daveey]

Each test pins one saving as a *work count* (statements run, rows walked, copies made), not a wall time, next to the
behaviour that must survive it.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_blocker_rows as rows
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_shared_blockers as ksb
from hermes_cli import kanban_task_change as tc
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import test_kanban_shared_blockers as helpers

kanban_home = helpers.kanban_home  # a covered board: the per-task row cache engages there


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_SESSION_ID", "HERMES_DELEGATED_CHILD_CONTEXT",
                "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    rows._reset_for_tests()
    tc._reset_for_tests()
    getattr(kwc, "_RESTORE_SEEN", {}).clear()
    yield h
    rows._reset_for_tests()
    tc._reset_for_tests()


# -- restore_missing_checks: a clock-only park that was looked at is not looked up again ---------------------------

def _clock_only_parks(conn, n):
    from hermes_cli import kanban_continuation as kc
    ids = []
    for i in range(n):
        tid = kb.create_task(conn, title=f"parked {i}", assignee="nobody")
        wake_at = int(time.time()) + 3600
        assert kb.schedule_task(conn, tid, reason=f"SCHEDULED_UNTIL={wake_at} cooldown", clock_only=True)
        assert kc.pending_wake(conn, tid) is not None
        ids.append(tid)
    return ids


def _event_statements(conn, fn):
    seen: list[str] = []
    conn.set_trace_callback(lambda sql: seen.append(sql) if "task_events" in sql else None)
    try:
        return fn(), seen
    finally:
        conn.set_trace_callback(None)


def test_restore_missing_checks_does_not_re_read_parks_it_already_settled(home):
    with kbc.connect_closing() as conn:
        ids = _clock_only_parks(conn, 12)
        assert not any(kwc.get_state(conn, t) for t in ids)  # no probe: every park is a candidate
        first, _ = _event_statements(conn, lambda: kwc.restore_missing_checks(conn))
        assert first == {"restored": [], "woken": []}
        second, seen = _event_statements(conn, lambda: kwc.restore_missing_checks(conn))
        assert second == {"restored": [], "woken": []}
        assert len(seen) <= 2, f"{len(seen)} task_events statements for 12 settled parks; want one batched read"


def test_a_park_that_arrives_after_the_batch_is_still_examined(home, monkeypatch):
    """The batched read only skips a card whose newest park is the one already settled: a re-park is new."""
    with kbc.connect_closing() as conn:
        (tid,) = _clock_only_parks(conn, 1)
        kwc.restore_missing_checks(conn)
        seen_key = next(iter(kwc._RESTORE_SEEN))
        old = kwc._RESTORE_SEEN[seen_key]
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        assert kb.claim_task(conn, tid) is not None
        assert kb.schedule_task(conn, tid, reason=f"SCHEDULED_UNTIL={int(time.time()) + 7200} again", clock_only=True)
        kwc.restore_missing_checks(conn)
        assert kwc._RESTORE_SEEN[seen_key] > old


def test_a_park_overtaken_by_a_later_run_is_not_re_read_every_tick(home, monkeypatch):
    """The ``claimed``/``spawned``-after-the-park skip is as permanent as the event: settle it like a clock-only park."""
    from hermes_cli import kanban_timed_park as ktp
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    with kbc.connect_closing() as conn:
        tids = []
        for i in range(8):
            tid = kb.create_task(conn, title=f"p{i}", assignee="worker")
            assert kb.claim_task(conn, tid, claimer="worker") is not None
            with kb.write_txn(conn):
                assert ktp.park(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z wait", requested_kind="time",
                                source_status="running", wake_check="true", check_every=300,
                                awaiting={"kind": "external", "ref": "x", "why": "y"})
            with kb.write_txn(conn):
                conn.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (tid,))
                kb._append_event(conn, tid, "claimed", {"run_id": 1})  # a later run, its park not here yet
            tids.append(tid)
        assert kwc.restore_missing_checks(conn) == {"restored": [], "woken": []}
        again, seen = _event_statements(conn, lambda: kwc.restore_missing_checks(conn))
        assert again == {"restored": [], "woken": []}
        assert len(seen) <= 2, f"{len(seen)} task_events statements for 8 parks a later run overtook"
        assert all(kwc.get_state(conn, t) is None for t in tids)


# -- blocker_states: copying a state is cheap and still private ---------------------------------------------------

def test_blocker_state_copy_is_equal_and_independent():
    import copy
    st = ksb.BlockerState("wall", "anthropic", "m", "l", 5, 3, ("t", 9), "run", 4, "why", {"a": 1}, {"b": 2})
    dup = copy.deepcopy(st)
    assert dup == st and dup is not st
    dup.behind["c"] = 3
    dup.probe_starts["d"] = 4
    assert st.behind == {"a": 1} and st.probe_starts == {"b": 2}
    assert copy.deepcopy({"wall": st})["wall"] == st


def test_blocker_state_copy_does_not_walk_fields_through_the_generic_deepcopy(monkeypatch):
    import copy
    st = ksb.BlockerState("wall", behind={"a": 1}, probe_starts={"b": 2})
    seen = []
    real = copy.deepcopy
    monkeypatch.setattr(copy, "deepcopy", lambda *a, **k: seen.append(a[0]) or real(*a, **k))
    ksb.BlockerState.__deepcopy__(st, {})
    assert seen == [], "the two int maps are shallow-copied; no field needs the recursive copier"


# -- blocker_rows.inputs: the sorted assertion rows are kept per held mapping -------------------------------------

def _assert_something(conn, name):
    return helpers._blocked_behind(conn, name)


def test_inputs_sort_the_assertion_rows_once_per_change_not_once_per_ask(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        a = _assert_something(conn, "a")
        _assert_something(conn, "b")
        now = int(conn.execute("SELECT MAX(ended_at) FROM task_runs").fetchone()[0]) + 1
        sorts = []
        real = sorted
        monkeypatch.setattr(rows, "sorted", lambda it, **k: (sorts.append(k) if k else None) or real(it, **k), raising=False)
        first, _ = rows.inputs(conn, now)
        again = [rows.inputs(conn, now)[0] for _ in range(4)]
        assert all([r.id for r in x] == [r.id for r in first] for x in again)
        assert len(sorts) <= 1, f"{len(sorts)} sorts for five asks of an unchanged board"
        kb.add_comment(conn, a, "u", "a write to a held task")
        rows.inputs(conn, now)
        assert len(sorts) <= 2
        assert [r.id for r in first] == sorted(r.id for r in first)


def test_inputs_window_still_follows_the_callers_now(kanban_home):
    with kbc.connect_closing() as conn:
        _assert_something(conn, "a")
        now = int(conn.execute("SELECT MAX(ended_at) FROM task_runs").fetchone()[0]) + 1
        inside, _ = rows.inputs(conn, now)
        assert len(inside) == 1
        outside, _ = rows.inputs(conn, now + ksb._LOOKBACK_SECONDS + 60)  # same held rows, a later window
        assert len(outside) == 0
        again, _ = rows.inputs(conn, now)
        assert [r.id for r in again] == [r.id for r in inside]
