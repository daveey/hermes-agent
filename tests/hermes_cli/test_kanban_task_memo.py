"""Per-task incremental memo for dispatcher structural scans (G2c, t_01a1db079d8d4505). [daveey]

``kanban_task_memo`` keeps ``task_id -> value`` per (file, name, extra key) and refreshes only the tasks the P2
change cursor (``kanban_task_change``) marks. Its consumers here: ``kanban_wake_check.foreign_ids`` (cards another
gateway created) and ``kanban_idle_candidates.candidate_task_ids`` (status + own event kinds). Every answer must equal
a fresh full scan, whatever the write sequence; anything the cursor cannot vouch for reads in full.
"""
from __future__ import annotations

import os
import random
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_activity_revision as rev
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_idle_candidates as cand
from hermes_cli import kanban_task_change as tc
from hermes_cli import kanban_task_memo as memo
from hermes_cli import kanban_wake_check as kwc

OWN, OTHER = "hub", "worker-7"
CREATED_ON = {"tasks": ("created_on",)}
FOREIGN_FULL = "SELECT id FROM tasks WHERE created_on IS NOT NULL"
CAND_FULL = "SELECT t.id FROM tasks t WHERE t.status"


@pytest.fixture(autouse=True)
def _fresh():
    memo.reset()
    tc._reset_for_tests()
    yield
    memo.reset()
    tc._reset_for_tests()


def _set_gateway(conn, gateway=OWN):
    """This gateway's id, as a synced board's ``kanban_sync_state`` row holds it (a minimal table here)."""
    with kbc.write_txn(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS kanban_sync_state (gateway_id TEXT)")
        if conn.execute("UPDATE kanban_sync_state SET gateway_id = ?", (gateway,)).rowcount == 0:
            conn.execute("INSERT INTO kanban_sync_state (gateway_id) VALUES (?)", (gateway,))


def _open(path):
    """A board connection whose ``tasks`` carry ``created_on`` (a synced board's column) and whose activity
    triggers watch it, with this gateway's id set."""
    conn = kbc.connect(path)
    if "created_on" not in [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]:
        with kbc.write_txn(conn):
            conn.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")
        rev.ensure_revision(conn, rev.LOCAL_TABLES)  # the update trigger now names the new column
    _set_gateway(conn)
    conn.close()
    return kbc.connect(path)  # reopen the initialized file with open-time identity attestation


@pytest.fixture
def board(tmp_path):
    conn = _open(tmp_path / "board.db")
    try:
        yield conn
    finally:
        conn.close()


def _write(conn, sql, params=()):
    with kbc.write_txn(conn):
        conn.execute(sql, params)


def _task(conn, created_on=None, status=None):
    tid = kb.create_task(conn, title="t", assignee="w")
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET created_on = ? WHERE id = ?", (created_on, tid))
        if status:
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    return tid


def _delete(conn, tid):
    with kbc.write_txn(conn):
        for table in ("task_events", "task_comments", "task_runs", "task_links"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            for col in ("task_id", "parent_id", "child_id"):
                if col in cols:
                    conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (tid,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))


def _truth_foreign(conn):
    return {r[0] for r in conn.execute(
        "SELECT id FROM tasks WHERE created_on IS NOT NULL AND created_on != ?", (OWN,))}


def _truth_cand(conn, statuses, kinds, exclude=False):
    op = "NOT IN" if exclude else "IN"
    return tuple(sorted(r[0] for r in conn.execute(
        f"SELECT id FROM tasks WHERE status {op} ({','.join('?' * len(statuses))}) AND id IN (SELECT task_id FROM "
        f"task_events WHERE kind IN ({','.join('?' * len(kinds))}))", (*statuses, *kinds))))


@pytest.mark.parametrize("managed", [False, True])
def test_retained_connection_cannot_poison_replacement(tmp_path, managed):
    path, replacement = tmp_path / "old.db", tmp_path / "new.db"
    c = _open(path)
    tid = _task(c, OTHER, "running")
    _write(c, "INSERT INTO task_events(task_id, kind, created_at) VALUES (?, ?, 1)",
           (tid, "remote_stop_requested"))
    c.close()
    # DELETE mode isolates the file-generation boundary from WAL sidecars.
    with sqlite3.connect(path) as setup:
        setup.execute("PRAGMA journal_mode=DELETE")
    shutil.copyfile(path, replacement)
    with sqlite3.connect(path) as original:
        original.execute("UPDATE tasks SET title='changed'")
    with sqlite3.connect(replacement) as newer:
        newer.execute("UPDATE tasks SET status='done', created_on=?", (OWN,))
    from hermes_cli import kanban_lockguard
    opening = kanban_lockguard.connect if managed else sqlite3.connect
    old = opening(path)
    old.execute("SELECT * FROM tasks").fetchall()
    os.replace(replacement, path)
    try:
        assert cand.candidate_task_ids(old, ("running",), ("remote_stop_requested",)) == (tid,)
        assert kwc.foreign_ids(old) == {tid}
        fresh = opening(path)
        try:
            assert cand.candidate_task_ids(fresh, ("running",), ("remote_stop_requested",)) == ()
            assert kwc.foreign_ids(fresh) == set()
        finally:
            fresh.close()
    finally:
        old.close()


def test_replacement_during_full_read_is_not_published(tmp_path):
    path, replacement = tmp_path / "board.db", tmp_path / "replacement.db"
    conn = _open(path)
    _task(conn, OTHER)
    conn.close()
    with sqlite3.connect(path) as setup:
        setup.execute("PRAGMA journal_mode=DELETE")
    shutil.copyfile(path, replacement)
    from hermes_cli.kanban_lockguard import connect
    conn = connect(path)
    try:
        def full(c):
            result = {r[0]: True for r in c.execute("SELECT id FROM tasks")}
            os.replace(replacement, path)
            return result
        result = memo.state("replace_during_read", conn, full=full,
                            partial=lambda c, ids: {}, columns=CREATED_ON)
        assert result.values
        assert result.cursor is None
        assert not memo._STATES
    finally:
        conn.close()


def test_new_file_and_raw_connections_never_publish(tmp_path):
    conn = kbc.connect(tmp_path / "new.db")
    try:
        assert conn._hermes_open_identity is None
        memo.state("new", conn, full=lambda c: {"a": True}, partial=lambda c, ids: {},
                   columns={"tasks": ("status",)})
        assert not memo._STATES
    finally:
        conn.close()
    with sqlite3.connect(tmp_path / "new.db") as raw:
        memo.state("raw", raw, full=lambda c: {"a": True}, partial=lambda c, ids: {},
                   columns={"tasks": ("status",)})
        assert not memo._STATES


class _Trace:
    def __init__(self, conn):
        self.conn, self.sql = conn, []

    def __enter__(self):
        self.conn.set_trace_callback(self.sql.append)
        return self

    def __exit__(self, *exc):
        self.conn.set_trace_callback(None)

    def count(self, prefix):
        return sum(s.startswith(prefix) for s in self.sql)


# --- foreign_ids --------------------------------------------------------------------------------------------------

def test_foreign_ids_reflect_created_on_change_insert_and_delete_after_one_call(board):
    mine, theirs = _task(board, OWN), _task(board, OTHER)
    assert kwc.foreign_ids(board) == {theirs}
    _write(board, "UPDATE tasks SET created_on = ? WHERE id = ?", (OTHER, mine))
    assert kwc.foreign_ids(board) == {mine, theirs}
    _write(board, "UPDATE tasks SET created_on = ? WHERE id = ?", (OWN, theirs))
    assert kwc.foreign_ids(board) == {mine}
    new = _task(board, OTHER)
    assert kwc.foreign_ids(board) == {mine, new}
    _delete(board, mine)
    assert kwc.foreign_ids(board) == {new}
    assert kwc.foreign_ids(board) == _truth_foreign(board)


def test_foreign_ids_never_rescan_the_board_on_an_unrelated_write(board):
    theirs = _task(board, OTHER)
    other = _task(board, OWN)
    assert kwc.foreign_ids(board) == {theirs}
    with _Trace(board) as t:
        for i in range(5):
            kb.add_comment(board, other, "x", f"churn {i}")
            assert kwc.foreign_ids(board) == {theirs}
    assert t.count(FOREIGN_FULL) == 0


def test_foreign_ids_are_keyed_by_own_gateway(board):
    a, b = _task(board, OWN), _task(board, OTHER)
    assert kwc.foreign_ids(board) == {b}
    assert kwc.foreign_ids(board, OTHER) == {a}
    _set_gateway(board, OTHER)
    assert kwc.foreign_ids(board) == {a}


# --- candidate_task_ids ---------------------------------------------------------------------------------------------

def test_candidates_reflect_an_event_insert_and_a_status_change_in_and_out(board):
    tid = _task(board, status="running")
    q = (("running",), ("remote_stop_requested",))
    assert cand.candidate_task_ids(board, *q) == ()
    with kbc.write_txn(board):
        kb._append_event(board, tid, "remote_stop_requested", {})
    assert cand.candidate_task_ids(board, *q) == (tid,)
    _write(board, "UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
    assert cand.candidate_task_ids(board, *q) == ()
    _write(board, "UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
    assert cand.candidate_task_ids(board, *q) == (tid,)
    _write(board, "DELETE FROM task_events WHERE task_id = ? AND kind = 'remote_stop_requested'", (tid,))
    assert cand.candidate_task_ids(board, *q) == ()
    # the exclude_statuses form is a separate memo
    with kbc.write_txn(board):
        kb._append_event(board, tid, "remote_stop_requested", {})
    assert cand.candidate_task_ids(board, ("archived",), q[1], exclude_statuses=True) == (tid,)
    assert cand.candidate_task_ids(board, ("running",), q[1], exclude_statuses=True) == ()


def test_a_heartbeat_kind_declaration_is_refused(board):
    with pytest.raises(ValueError, match="heartbeat"):
        cand.candidate_task_ids(board, ("running",), ("heartbeat",))
    with pytest.raises(ValueError):
        memo.state("x", board, full=lambda c: {}, partial=lambda c, ids: {}, columns=CREATED_ON,
                   event_kinds=rev.UNREAD_EVENT_KINDS)
    with pytest.raises(ValueError, match="bookkeeping"):
        memo.state("x", board, full=lambda c: {}, partial=lambda c, ids: {}, columns={"tasks": ("claim_expires",)})


def test_a_column_added_after_the_triggers_is_never_trusted(tmp_path):
    """A sync migration's ``ADD COLUMN created_on`` on an open board: the update trigger does not watch it until a
    writer init reinstalls the triggers, so its changes mark nothing and every call must read in full."""
    conn = kbc.connect(tmp_path / "board.db")
    conn.close()
    conn = kbc.connect(tmp_path / "board.db")
    try:
        _set_gateway(conn)
        with kbc.write_txn(conn):
            conn.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")  # no ensure_revision afterwards
        tid = _task(conn, OTHER)
        assert kwc.foreign_ids(conn) == {tid}
        _write(conn, "UPDATE tasks SET created_on = ? WHERE id = ?", (OWN, tid))  # marks nothing
        assert kwc.foreign_ids(conn) == frozenset()
        assert not any(k[1] == "foreign_ids" for k in memo._STATES)
        rev.ensure_revision(conn, rev.LOCAL_TABLES)  # the next writer init watches the column
        _write(conn, "UPDATE tasks SET created_on = ? WHERE id = ?", (OTHER, tid))
        assert kwc.foreign_ids(conn) == {tid}
        _write(conn, "UPDATE tasks SET created_on = ? WHERE id = ?", (OWN, tid))
        with _Trace(conn) as t:
            assert kwc.foreign_ids(conn) == frozenset()
        assert t.count(FOREIGN_FULL) == 0  # trusted again: the partial read found it
    finally:
        conn.close()


# --- transactions ---------------------------------------------------------------------------------------------------

def test_a_rolled_back_write_is_seen_inside_the_txn_never_stored_and_its_revision_reuse_is_caught(board):
    theirs = _task(board, OTHER)
    assert kwc.foreign_ids(board) == {theirs}
    slot = next(k for k in memo._STATES if k[1] == "foreign_ids")
    stored = memo._STATES[slot]
    board.execute("BEGIN IMMEDIATE")
    try:
        board.execute("INSERT INTO tasks (id, title, status, created_at, created_on) "
                      "VALUES ('t_rolled', 'r', 'todo', 1, ?)", (OTHER,))
        assert kwc.foreign_ids(board) == {theirs, "t_rolled"}  # the caller sees its own write
        assert memo._STATES[slot] is stored                    # ...and nothing was stored
    finally:
        board.rollback()
    assert kwc.foreign_ids(board) == {theirs}
    # The next write reuses the rolled-back revision number on another task.
    rolled_rev = stored.cursor.revision + 1
    reused = _task(board, OTHER)
    assert board.execute("SELECT rev FROM kanban_task_change WHERE task_id = ?", (reused,)).fetchone()[0] >= rolled_rev
    assert kwc.foreign_ids(board) == {theirs, reused} == _truth_foreign(board)


def test_a_current_stored_pair_serves_a_read_inside_a_write_txn(board):
    """``recompute_ready`` calls foreign_ids inside ``write_txn``: with the pair current, no full scan runs."""
    theirs = _task(board, OTHER)
    assert kwc.foreign_ids(board) == {theirs}
    with _Trace(board) as t:
        with kbc.write_txn(board):
            assert kwc.foreign_ids(board) == {theirs}
    assert t.count(FOREIGN_FULL) == 0


# --- untrusted cursors read in full ---------------------------------------------------------------------------------

def test_full_mark_reads_in_full(board):
    theirs = _task(board, OTHER)
    assert kwc.foreign_ids(board) == {theirs}
    with kbc.write_txn(board):
        board.execute(rev._BUMP)
        board.execute(rev._mark("NULL"))
    with _Trace(board) as t:
        assert kwc.foreign_ids(board) == {theirs}
    assert t.count(FOREIGN_FULL) == 1


def test_a_schema_change_reads_in_full(board):
    theirs = _task(board, OTHER)
    assert kwc.foreign_ids(board) == {theirs}
    _write(board, "CREATE TABLE t01a1db07_unrelated (x)")
    with _Trace(board) as t:
        assert kwc.foreign_ids(board) == {theirs}
    assert t.count(FOREIGN_FULL) == 1


def test_a_shm_identity_change_reads_in_full(board, monkeypatch):
    from hermes_cli import kanban_lockguard
    theirs = _task(board, OTHER)
    assert kwc.foreign_ids(board) == {theirs}
    monkeypatch.setattr(kanban_lockguard, "shm_identity", lambda _path: ("new-wal-generation",))
    with _Trace(board) as t:
        assert kwc.foreign_ids(board) == {theirs}
    assert t.count(FOREIGN_FULL) == 1
    with _Trace(board) as t:
        assert kwc.foreign_ids(board) == {theirs}
    assert t.count(FOREIGN_FULL) == 0  # re-armed on the new generation


def test_a_replaced_file_reads_in_full(tmp_path):
    path = tmp_path / "board.db"
    conn = _open(path)
    first = _task(conn, OTHER)
    assert kwc.foreign_ids(conn) == {first}
    conn.close()
    other = _open(tmp_path / "other.db")
    second = _task(other, OTHER)
    other.close()
    for suffix in ("-wal", "-shm"):
        for p in (Path(str(path) + suffix), Path(str(tmp_path / "other.db") + suffix)):
            if p.exists():
                p.unlink()
    shutil.copy(tmp_path / "other.db", tmp_path / "staged.db")
    os.replace(tmp_path / "staged.db", path)  # a new inode at the same path
    conn = kbc.connect(path)
    try:
        assert kwc.foreign_ids(conn) == {second}
    finally:
        conn.close()


def test_an_untrusted_cursor_never_stores(board):
    theirs = _task(board, OTHER)
    rev.drop_triggers(board)
    with _Trace(board) as t:
        for _ in range(3):
            assert kwc.foreign_ids(board) == {theirs}
    assert t.count(FOREIGN_FULL) == 3
    assert not any(k[1] == "foreign_ids" for k in memo._STATES)


# --- concurrency ----------------------------------------------------------------------------------------------------

def test_a_slower_thread_never_moves_the_pair_backwards(tmp_path):
    path = tmp_path / "board.db"
    main = _open(path)
    first = _task(main, OTHER)
    entered, release = threading.Event(), threading.Event()
    slow_thread = {}

    def full(c):
        if threading.current_thread() is slow_thread.get("t"):
            entered.set()
            assert release.wait(10)
        return {r[0]: True for r in c.execute(
            "SELECT id FROM tasks WHERE created_on IS NOT NULL AND created_on != ?", (OWN,))}

    def partial(c, ids):
        return {r[0]: True for r in c.execute(
            f"SELECT id FROM tasks WHERE id IN ({','.join('?' * len(ids))}) AND created_on IS NOT NULL "
            "AND created_on != ?", (*ids, OWN))}

    out = {}

    def slow():
        conn = kbc.connect(path)
        try:
            out["slow"] = memo.state("race", conn, full=full, partial=partial, columns=CREATED_ON)
        finally:
            conn.close()

    t = threading.Thread(target=slow)
    slow_thread["t"] = t
    t.start()
    assert entered.wait(10)            # the slow reader holds a snapshot at the old revision
    second = _task(main, OTHER)        # a writer moves on
    fast = memo.state("race", main, full=full, partial=partial, columns=CREATED_ON)
    assert set(fast.values) == {first, second}
    release.set()
    t.join(10)
    assert set(out["slow"].values) == {first}  # its own snapshot
    slot = next(k for k in memo._STATES if k[1] == "race")
    kept = memo._STATES[slot].cursor
    assert kept is not None and fast.cursor is not None
    assert kept.revision == fast.cursor.revision  # not moved backwards
    assert set(memo.state("race", main, full=full, partial=partial, columns=CREATED_ON).values) == {first, second}
    main.close()


def test_concurrent_readers_and_a_writer_stay_equal_to_a_full_scan(tmp_path):
    path = tmp_path / "board.db"
    main = _open(path)
    stop, errors = threading.Event(), []
    seen: list[int] = []

    def reader():
        conn = kbc.connect(path)
        try:
            while not stop.is_set():
                kwc.foreign_ids(conn)
                slot = next((k for k in memo._STATES if k[1] == "foreign_ids"), None)
                if slot is not None:
                    seen.append(memo._STATES[slot].cursor.revision)
        except Exception as exc:  # health: allow BLE001 -- surfaced by the assertion below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    try:
        for i in range(40):
            _task(main, OTHER if i % 2 else OWN)
    finally:
        stop.set()
        for t in threads:
            t.join(10)
    assert not errors
    assert kwc.foreign_ids(main) == _truth_foreign(main)
    main.close()


# --- randomized equivalence -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", [1, 2, 3])
def test_memo_equals_a_fresh_full_scan_over_random_write_sequences(board, seed):
    rng = random.Random(seed)
    kinds = ("remote_stop_requested", "owner_archive_requested")
    statuses = ("todo", "ready", "running", "done", "archived")
    queries = ((("running",), (kinds[0],), False), (("archived",), (kinds[1],), True),
               (("done", "archived"), kinds, False))
    tasks = [_task(board, rng.choice((OWN, OTHER, None)), rng.choice(statuses)) for _ in range(6)]
    for step in range(120):
        op = rng.randrange(8)
        tid = rng.choice(tasks) if tasks else None
        in_txn_rollback = rng.random() < 0.1
        if in_txn_rollback:
            board.execute("BEGIN IMMEDIATE")
        try:
            if op == 0 or tid is None:
                if in_txn_rollback:
                    board.execute("INSERT INTO tasks (id, title, status, created_at, created_on) VALUES "
                                  "(?, 't', 'todo', 1, ?)", (f"t_rb{step}", OTHER))
                else:
                    tasks.append(_task(board, rng.choice((OWN, OTHER, None)), rng.choice(statuses)))
            elif op == 1:
                sql, args = "UPDATE tasks SET created_on = ? WHERE id = ?", (rng.choice((OWN, OTHER, None)), tid)
                board.execute(sql, args) if in_txn_rollback else _write(board, sql, args)
            elif op == 2:
                sql, args = "UPDATE tasks SET status = ? WHERE id = ?", (rng.choice(statuses), tid)
                board.execute(sql, args) if in_txn_rollback else _write(board, sql, args)
            elif op == 3:
                if in_txn_rollback:
                    kb._append_event(board, tid, rng.choice(kinds), {})
                else:
                    with kbc.write_txn(board):
                        kb._append_event(board, tid, rng.choice(kinds), {})
            elif op == 4 and not in_txn_rollback:
                _delete(board, tid)
                tasks.remove(tid)
            elif op == 5 and not in_txn_rollback:
                _write(board, "DELETE FROM task_events WHERE task_id = ? AND kind IN (?, ?)", (tid, *kinds))
            elif op == 6 and not in_txn_rollback:
                kb.add_comment(board, tid, "x", "noise")
            elif not in_txn_rollback:
                with kbc.write_txn(board):  # a heartbeat: never marks, never read by these consumers
                    kb._append_event(board, tid, "heartbeat", {})
            if in_txn_rollback:  # read-through inside the txn sees the uncommitted state
                assert kwc.foreign_ids(board) == _truth_foreign(board)
        finally:
            if in_txn_rollback:
                board.rollback()
        assert kwc.foreign_ids(board) == _truth_foreign(board), step
        for statuses_q, kinds_q, exclude in queries:
            assert tuple(sorted(cand.candidate_task_ids(board, statuses_q, kinds_q, exclude_statuses=exclude))) == \
                _truth_cand(board, statuses_q, kinds_q, exclude), step


def test_memory_is_bounded(board, monkeypatch):
    monkeypatch.setattr(memo, "MAX_SLOTS", 3)
    for i in range(10):
        memo.state(f"slot{i}", board, full=lambda c: {"a": True}, partial=lambda c, ids: {}, columns=CREATED_ON)
        assert len(memo._STATES) <= 3
    monkeypatch.setattr(memo, "MAX_ENTRIES", 1)
    memo.reset()
    memo.state("big", board, full=lambda c: {"a": True, "b": True}, partial=lambda c, ids: {}, columns=CREATED_ON)
    assert not memo._STATES
    memo.state("falsy", board, full=lambda c: {"a": 0, "b": None, "c": True}, partial=lambda c, ids: {},
               columns=CREATED_ON)
    assert dict(next(iter(memo._STATES.values())).values) == {"c": True}


# --- restore_missing_checks stays live on task_wake_checks ----------------------------------------------------------

def test_a_deleted_probe_is_still_re_registered_with_the_memo_warm(tmp_path, monkeypatch):
    from hermes_cli import kanban_timed_park as ktp
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    getattr(kwc, "_RESTORE_SEEN", {}).clear()
    try:
        with kbc.connect_closing() as conn:
            if "created_on" not in [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]:
                with kbc.write_txn(conn):
                    conn.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")
                rev.ensure_revision(conn, rev.LOCAL_TABLES)
            _set_gateway(conn)
            tid = kb.create_task(conn, title="t", assignee="worker")
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='ready', created_on = ? WHERE id=?", (OWN, tid))
            assert kb.claim_task(conn, tid, claimer="worker") is not None
            probe = f"test -f {tmp_path / 'flag'}"
            with kb.write_txn(conn):
                assert ktp.park(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z wait", requested_kind="time",
                                source_status="running", wake_check=probe, check_every=300,
                                awaiting={"kind": "external", "ref": "build", "why": "a build writes the flag"})
            assert kwc.get_state(conn, tid) is not None
            assert kwc.foreign_ids(conn) == frozenset()  # warm the memo
            assert kwc.restore_missing_checks(conn) == {"restored": [], "woken": []}
            with kb.write_txn(conn):  # task_wake_checks is unwatched: this marks no task
                conn.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (tid,))
            assert kwc.restore_missing_checks(conn) == {"restored": [tid], "woken": []}
            assert kwc.get_state(conn, tid)["cmd"] == probe
    finally:
        kwc._shutdown_for_tests()
