"""G2c: per-task decoded-row cache and the per-pass shared walk of ``blocker_states``. [daveey]

Design t_01a1d563324d7e8e §1 option 1 / §2 P2 / R13: ``kanban_blocker_rows`` keeps each asserting task's decoded runs
across passes and re-reads only the tasks ``kanban_task_change.changed_since`` names; the walk itself, check clears
and the fence rerun every pass. Every test compares the cached derivation with a cold (uncached) one.
"""
from __future__ import annotations

import dataclasses
import json
import random
import sqlite3
import threading

import pytest

from hermes_cli import kanban_blocker_inputs as inputs
from hermes_cli import kanban_blocker_rows as rows
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_idle_candidates as idle
from hermes_cli import kanban_shared_blockers as ksb
from hermes_cli import kanban_task_change as tc
from tests.hermes_cli import test_kanban_shared_blockers as helpers

kanban_home = helpers.kanban_home
BID = helpers.BID
DAY = 86400


@pytest.fixture(autouse=True)
def _fresh_caches():
    rows._reset_for_tests()
    tc._reset_for_tests()
    yield
    rows._reset_for_tests()
    tc._reset_for_tests()


def _snap(states) -> dict:
    return {bid: dataclasses.asdict(st) for bid, st in sorted(states.items())}


def cached(conn, now) -> dict:
    return _snap(ksb.blocker_states(conn, now=now))


def cold(conn, now) -> dict:
    """The same derivation with no retained rows (and nothing retained afterwards)."""
    with rows._LOCK:
        saved = dict(rows._STATES)
        rows._STATES.clear()
    try:
        return _snap(ksb.blocker_states(conn, now=now))
    finally:
        with rows._LOCK:
            rows._STATES.clear()
            rows._STATES.update(saved)


def _same(conn, now) -> dict:
    want = cold(conn, now)
    got = cached(conn, now)
    assert got == want
    return got


@pytest.fixture
def full_reads(monkeypatch):
    calls = []
    real = rows._read_all

    def spy(conn, cutoff):
        calls.append(cutoff)
        return real(conn, cutoff)
    monkeypatch.setattr(rows, "_read_all", spy)
    return calls


def _now(conn) -> int:
    return int(conn.execute("SELECT MAX(ended_at) FROM task_runs").fetchone()[0]) + 1


def _finish_clean(conn, tid) -> None:
    """``tid`` is unblocked, claimed and completes without hitting the wall (a clearing run)."""
    assert kb.unblock_task(conn, tid)
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    conn.commit()
    assert kb.claim_task(conn, tid) is not None
    assert kb.complete_task(conn, tid, result="ok")


def _age_task(conn, tid, seconds) -> None:
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET started_at = started_at - ?, ended_at = CASE WHEN ended_at IS NULL "
                     "THEN NULL ELSE ended_at - ? END WHERE task_id = ?", (seconds, seconds, tid))


def test_the_cache_is_incremental_on_a_covered_board(kanban_home, full_reads):
    """Precondition for every test below: without it they would only exercise the cold path."""
    with kbc.connect_closing() as conn:
        helpers._blocked_behind(conn, "a")
        now = _now(conn)
        cached(conn, now)
        assert len(full_reads) == 1
        kb.add_comment(conn, kb.create_task(conn, title="x", assignee="default"), "u", "body")
        cached(conn, now)
        assert len(full_reads) == 1


def test_a_write_to_task_b_changes_task_a_derivation(kanban_home, full_reads):
    """Cross-task: only B is written; A's behind/cleared/applicable come from B's run."""
    with kbc.connect_closing() as conn:
        a = helpers._blocked_behind(conn, "a")
        b = helpers._blocked_behind(conn, "b")
        now = _now(conn) + 5
        before = _same(conn, now)
        assert set(before[BID]["behind"]) == {a, b} and before[BID]["cleared_at"] == 0
        _finish_clean(conn, b)
        after = _same(conn, _now(conn) + 5)
        assert after[BID]["cleared_by"][0] == b and set(after[BID]["behind"]) == {a}
        assert len(full_reads) == 3   # two cold comparisons and the first fill: only B was re-read after


def test_an_assertion_leaves_the_window_with_no_write(kanban_home):
    with kbc.connect_closing() as conn:
        helpers._blocked_behind(conn, "a")
        now = _now(conn)
        assert BID in _same(conn, now)
        assert _same(conn, now + ksb._LOOKBACK_SECONDS + 1) == {}
        assert BID in _same(conn, now)


def test_a_caller_now_earlier_than_the_fill(kanban_home, full_reads):
    """A task whose assertions are outside the fill's window is not held; an earlier caller window needs it."""
    with kbc.connect_closing() as conn:
        old = helpers._blocked_behind(conn, "old")
        helpers._blocked_behind(conn, "new")
        _age_task(conn, old, 20 * DAY)
        now = _now(conn)
        assert set(_same(conn, now)[BID]["behind"]) != {old}
        assert old not in _same(conn, now)[BID]["behind"]
        earlier = now - 10 * DAY
        got = _same(conn, earlier)
        assert old in got[BID]["behind"]


def test_an_alias_change_with_no_board_write(kanban_home, monkeypatch):
    from hermes_cli import config
    cfg = {"kanban": {"shared_blocker_aliases": {}}}
    monkeypatch.setattr(config, "load_config_readonly", lambda: cfg)
    with kbc.connect_closing() as conn:
        helpers._blocked_behind(conn, "a", blocker_id="old-wall")
        now = _now(conn)
        assert set(_same(conn, now)) == {"old-wall"}
        monkeypatch.setattr(config, "load_config_readonly",
                            lambda: {"kanban": {"shared_blocker_aliases": {"old-wall": "new-wall"}}})
        assert set(_same(conn, now)) == {"new-wall"}


def test_equal_ended_at_tie_goes_to_the_higher_run_id(kanban_home):
    """The asserting rows are rebuilt from per-task entries; they must be re-sorted by run id across tasks."""
    with kbc.connect_closing() as conn:
        first = kb.create_task(conn, title="first", assignee="default")
        second = kb.create_task(conn, title="second", assignee="default")
        # ``second`` asserts first (lower run id), ``first`` last (higher run id): task order != run order.
        for tid, why in ((second, "older run"), (first, "newer run")):
            assert kb.claim_task(conn, tid) is not None
            assert kb.block_task(conn, tid, reason=why, kind="capability", blocker_id=BID)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET ended_at = (SELECT MAX(ended_at) FROM task_runs)")
        now = _now(conn)
        assert _same(conn, now)[BID]["reason"] == "newer run"
        for _ in range(2):
            assert cached(conn, now)[BID]["reason"] == "newer run"


@pytest.mark.parametrize("synced", [False, True])
def test_a_late_heartbeat_after_condition_met(kanban_home, synced):
    """condition_met evidence is read live: a heartbeat of the asserting run (which never marks a task) that lands
    after the condition_met event hides it, cached and uncached alike."""
    with kbc.connect_closing() as conn:
        a = helpers._blocked_behind(conn, "a")
        helpers._blocked_behind(conn, "b")
        run = conn.execute("SELECT MAX(id) FROM task_runs WHERE task_id = ?", (a,)).fetchone()[0]
        now = _now(conn) + 5
        _same(conn, now)
        with kb.write_txn(conn):
            kb._append_event(conn, a, ksb.CONDITION_MET_EVENT, {"rc": 0})
        met = _same(conn, now)
        assert met[BID]["cleared_via"] == ksb.CONDITION_MET_EVENT
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                         "VALUES (?, ?, 'heartbeat', ?, ?)", (a, run, json.dumps({"synced": synced}), now))
        assert _same(conn, now)[BID]["cleared_via"] != ksb.CONDITION_MET_EVENT


def test_a_rolled_back_write_never_seeds_the_cache(kanban_home):
    """A result read inside a transaction is not stored: after a rollback the next write reuses the revision."""
    with kbc.connect_closing() as conn:
        a = helpers._blocked_behind(conn, "a")
        b = helpers._blocked_behind(conn, "b")
        now = _now(conn) + 5
        _same(conn, now)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE task_runs SET metadata = NULL, outcome = 'completed' WHERE task_id = ?", (a,))
        inside = cached(conn, now)
        assert a not in inside[BID]["behind"]
        conn.execute("ROLLBACK")
        rev_before = conn.execute("SELECT revision FROM kanban_activity_revision").fetchone()[0]
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET title = 'b2' WHERE id = ?", (b,))
        assert conn.execute("SELECT revision FROM kanban_activity_revision").fetchone()[0] == rev_before + 1
        got = _same(conn, now)
        assert a in got[BID]["behind"]


@pytest.mark.parametrize("case", ["full_mark", "file_replace", "shm", "task_delete"])
def test_untrusted_cursors_and_deletes(kanban_home, full_reads, monkeypatch, tmp_path, case):
    with kbc.connect_closing() as conn:
        a = helpers._blocked_behind(conn, "a")
        helpers._blocked_behind(conn, "b")
        now = _now(conn) + 5
        snapshot = sqlite3.connect(tmp_path / "snap.db")
        conn.backup(snapshot)
        _same(conn, now)
        reads = len(full_reads)
        if case == "full_mark":
            from hermes_cli import kanban_activity_revision as rev
            with kb.write_txn(conn):
                conn.execute(rev._BUMP)
                conn.execute(rev._mark("NULL"))
        elif case == "file_replace":
            _finish_clean(conn, a)
            cached(conn, _now(conn) + 5)
            reads = len(full_reads)
            snapshot.backup(conn)   # restored in place: same inode, regressed revision
        elif case == "shm":
            from hermes_cli import kanban_lockguard
            monkeypatch.setattr(kanban_lockguard, "shm_identity", lambda _path: ("another-wal-generation",))
        else:
            with kb.write_txn(conn):
                conn.execute("DELETE FROM task_events WHERE task_id = ?", (a,))
                conn.execute("DELETE FROM task_runs WHERE task_id = ?", (a,))
                conn.execute("DELETE FROM tasks WHERE id = ?", (a,))
        snapshot.close()
        got = _same(conn, now if case != "file_replace" else _now(conn) + 5)
        if case == "task_delete":
            assert a not in got[BID]["behind"]
            assert len(full_reads) == reads + 1   # the cold comparison only: the cache stayed incremental
        else:
            assert len(full_reads) >= reads + 2   # the cold comparison and the cache's own FULL
        if case == "file_replace":
            assert a in got[BID]["behind"]


def test_two_threads_racing_writes_and_reads(kanban_home):
    with kbc.connect_closing() as conn:
        tids = [helpers._blocked_behind(conn, f"t{i}") for i in range(6)]
        now = _now(conn) + 5
        stop = threading.Event()
        errors: list[BaseException] = []

        def reader():
            with kbc.connect_closing() as rc:
                while not stop.is_set():
                    try:
                        ksb.blocker_states(rc, now=now)
                    except sqlite3.OperationalError:
                        pass   # busy: the race is the point, not lock waits
                    except BaseException as exc:
                        errors.append(exc)
                        return

        threads = [threading.Thread(target=reader) for _ in range(2)]
        for t in threads:
            t.start()
        try:
            for tid in tids[:3]:
                _finish_clean(conn, tid)
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET title = title || '!' WHERE id = ?", (tids[-1],))
        finally:
            stop.set()
            for t in threads:
                t.join()
        assert not errors
        _same(conn, _now(conn) + 5)


def test_store_never_moves_backwards(kanban_home):
    with kbc.connect_closing() as conn:
        helpers._blocked_behind(conn, "a")
        now = _now(conn)
        cached(conn, now)
        path = rows._main_path(conn)
        newer = rows._STATES[path]
        older = dataclasses.replace(newer, cursor=dataclasses.replace(newer.cursor, revision=newer.cursor.revision - 1),
                                    tasks={})
        rows._store(path, older)
        assert rows._STATES[path] is newer


def test_randomized_cached_equals_uncached(kanban_home):
    rng = random.Random(20261114)
    with kbc.connect_closing() as conn:
        tids = [kb.create_task(conn, title=f"t{i}", assignee="default") for i in range(6)]
        walls = [BID, "other-wall", "third-wall"]
        for _step in range(60):
            tid = rng.choice(tids)
            status = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
            op = rng.random()
            if status in ("ready", "todo") and op < 0.6:
                assert kb.claim_task(conn, tid) is not None
                if rng.random() < 0.7:
                    kb.block_task(conn, tid, reason=f"wall {_step}", kind="capability", blocker_id=rng.choice(walls))
                else:
                    kb.complete_task(conn, tid, result="ok")
            elif status == "blocked" and op < 0.5:
                kb.unblock_task(conn, tid)
            elif status == "done" and op < 0.2:
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            elif op < 0.7:
                _age_task(conn, tid, rng.choice([DAY, 5 * DAY]))
            else:
                kb.add_comment(conn, tid, "u", f"c{_step}")
            base = _now(conn) if conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] else 2000000000
            for when in (base + 1, base - 3 * DAY, base + 13 * DAY):
                _same(conn, when)


def test_the_shared_walk_is_reused_within_a_pass_and_dropped_by_a_lane_write(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        a = helpers._blocked_behind(conn, "a")
        helpers._blocked_behind(conn, "b")
        now = _now(conn) + 5
        walks = []
        real = ksb._walk

        def spy(*args):
            walks.append(1)
            return real(*args)
        monkeypatch.setattr(ksb, "_walk", spy)

        @inputs.input_pass
        def one_pass():
            first = ksb.blocker_states(conn, now=now)
            first[BID].behind.clear()   # a consumer's private copy: never seen by the next lane
            second = ksb.blocker_states(conn, now=now)
            assert a in second[BID].behind and len(walks) == 1
            with kb.write_txn(conn):   # a lane write in the same pass
                conn.execute("UPDATE task_runs SET metadata = NULL, outcome = 'completed' WHERE task_id = ?", (a,))
            third = ksb.blocker_states(conn, now=now)
            assert len(walks) == 2 and a not in third[BID].behind
        one_pass()


def test_a_shared_walk_is_keyed_by_the_callers_window(kanban_home):
    """Lanes pass different ``now``: one pass must not hand a later window the walk of an earlier one."""
    with kbc.connect_closing() as conn:
        helpers._blocked_behind(conn, "a")
        now = _now(conn)

        @inputs.input_pass
        def one_pass():
            assert BID in ksb.blocker_states(conn, now=now)
            assert ksb.blocker_states(conn, now=now + ksb._LOOKBACK_SECONDS + 1) == {}
            assert BID in ksb.blocker_states(conn, now=now)
        one_pass()


def test_held_rows_are_bounded_to_tasks_with_assertions(kanban_home):
    with kbc.connect_closing() as conn:
        a = helpers._blocked_behind(conn, "a")
        b = helpers._blocked_behind(conn, "b")
        now = _now(conn)
        cached(conn, now)
        path = rows._main_path(conn)
        assert set(rows._STATES[path].tasks) == {a, b}
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET metadata = NULL WHERE task_id = ?", (a,))
        cached(conn, now)
        assert set(rows._STATES[path].tasks) == {b}


def test_the_sweep_shares_one_walk_across_lanes(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        helpers._blocked_behind(conn, "a")
        walks = []
        real = ksb._walk

        def spy(*args):
            walks.append(1)
            return real(*args)
        monkeypatch.setattr(ksb, "_walk", spy)

        class Result:
            pass
        idle.sweep_shared_blockers(conn, Result())
        assert len(walks) == 1


def test_foreign_among_matches_foreign_ids(kanban_home, monkeypatch):
    from hermes_cli import kanban_wake_check as kwc
    with kbc.connect_closing() as conn:
        ids = [kb.create_task(conn, title=f"t{i}", assignee="default") for i in range(4)]
        assert rows.foreign_among(conn, ids) == frozenset()   # no gateway: nothing is foreign
        if "created_on" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}:
            conn.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")
        monkeypatch.setattr(kwc, "own_gateway", lambda _conn: "home")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET created_on = ? WHERE id = ?", ("worker", ids[0]))
            conn.execute("UPDATE tasks SET created_on = ? WHERE id = ?", ("home", ids[1]))
        assert rows.foreign_among(conn, ids) == frozenset(kwc.foreign_ids(conn)) & set(ids) == {ids[0]}
        assert rows.foreign_among(conn, ids[1:]) == frozenset()
