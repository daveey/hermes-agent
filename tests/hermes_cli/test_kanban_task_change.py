"""Per-task change cursor (P2) on boards and sync shadows (t_01a1d59d3ef805c4). [daveey]

Design t_01a1d563324d7e8e §2 P2 with review R7/R8: every content write marks the task(s) it touched in
``kanban_task_change`` at the revision it bumped, so ``changed_since`` answers incrementally; a cursor is trusted only
while the file can vouch for it (coverage, MAX(rev) == revision, no regression, same file identity and schema),
otherwise the answer is FULL.
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_activity_revision as rev
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_task_change as tc
from hermes_cli.kanban_db_dispatch import heartbeat_worker
from tests.hermes_cli import kanban_sync_support as support

FULL = tc.FULL


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    tc._reset_for_tests()
    yield home
    tc._reset_for_tests()


@pytest.fixture
def board(home):
    conn = kbc.connect(home / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def shadow(home):
    conn = kbc.connect(home / "kanban" / sync.SHADOW_DIR / "default.db")
    try:
        yield conn
    finally:
        conn.close()


def _armed(conn) -> tc.Cursor:
    """A trusted cursor at the current revision (the first call is always FULL)."""
    first, cursor = tc.changed_since(conn, None)
    assert first is FULL and cursor is not None
    again, cursor = tc.changed_since(conn, cursor)
    assert again == frozenset() and cursor is not None
    return cursor


def _write(conn, sql, params=()):
    with kbc.write_txn(conn):
        conn.execute(sql, params)


def test_a_task_write_marks_only_that_task_and_rearms(board):
    a = kb.create_task(board, title="a", assignee="w")
    b = kb.create_task(board, title="b", assignee="w")
    cursor = _armed(board)
    _write(board, "UPDATE tasks SET title = 'renamed' WHERE id = ?", (a,))
    changed, cursor = tc.changed_since(board, cursor)
    assert changed == {a}
    assert tc.changed_since(board, cursor)[0] == frozenset()
    assert b not in changed


def test_insert_or_replace_is_marked_by_the_insert_trigger(board):
    """R7: REPLACE deletes the old row without firing DELETE triggers (no recursive_triggers); the INSERT upsert
    still marks the task."""
    tid = kb.create_task(board, title="a", assignee="w")
    cursor = _armed(board)
    row = dict(board.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
    row["title"] = "replaced"
    cols = ", ".join(row)
    _write(board, f"INSERT OR REPLACE INTO tasks ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    changed, _ = tc.changed_since(board, cursor)
    assert changed == {tid}


def test_a_link_marks_both_ends(board):
    parent = kb.create_task(board, title="parent", assignee="w")
    child = kb.create_task(board, title="child", assignee="w")
    other = kb.create_task(board, title="other", assignee="w")
    cursor = _armed(board)
    _write(board, "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, child))
    changed, cursor = tc.changed_since(board, cursor)
    assert changed == {parent, child}
    _write(board, "UPDATE task_links SET parent_id = ? WHERE parent_id = ? AND child_id = ?", (other, parent, child))
    changed, cursor = tc.changed_since(board, cursor)
    assert changed == {parent, other, child}  # the old parent loses a child: it moved too
    _write(board, "DELETE FROM task_links WHERE child_id = ?", (child,))
    assert tc.changed_since(board, cursor)[0] == {other, child}


def test_a_delete_marks_the_task_and_readers_see_it_gone(board):
    tid = kb.create_task(board, title="doomed", assignee="w")
    keep = kb.create_task(board, title="keep", assignee="w")
    cursor = _armed(board)
    assert kb.delete_task(board, tid)
    changed, _ = tc.changed_since(board, cursor)
    assert tid in changed and keep not in changed
    assert board.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone() is None  # missing = deleted


def test_comments_runs_and_events_mark_their_task(board):
    tid = kb.create_task(board, title="a", assignee="w")
    cursor = _armed(board)
    _write(board, "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'me', 'hi', 1)", (tid,))
    changed, cursor = tc.changed_since(board, cursor)
    assert changed == {tid}
    _write(board, "INSERT INTO task_events (task_id, kind, created_at) VALUES (?, 'completed', 1)", (tid,))
    changed, cursor = tc.changed_since(board, cursor)
    assert changed == {tid}
    _write(board, "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?, 'w', 'running', 1)", (tid,))
    assert tc.changed_since(board, cursor)[0] == {tid}


def test_a_row_moved_to_another_task_marks_both(board):
    a = kb.create_task(board, title="a", assignee="w")
    b = kb.create_task(board, title="b", assignee="w")
    _write(board, "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'me', 'hi', 1)", (a,))
    cursor = _armed(board)
    _write(board, "UPDATE task_comments SET task_id = ? WHERE task_id = ?", (b, a))
    assert tc.changed_since(board, cursor)[0] == {a, b}


def test_heartbeats_and_bookkeeping_do_not_mark(board):
    """R8: the same exclusions as the board revision: claim/lease/heartbeat columns and heartbeat events."""
    tid = kb.create_task(board, title="a", assignee="w")
    assert kb.claim_task(board, tid) is not None
    cursor = _armed(board)
    before = board.execute("SELECT revision FROM kanban_activity_revision").fetchone()[0]
    assert heartbeat_worker(board, tid, note="alive")  # task + run heartbeat columns and a heartbeat event
    _write(board, "UPDATE tasks SET claim_expires = 99, worker_pid = 7, lease_expires = 5 WHERE id = ?", (tid,))
    _write(board, "UPDATE task_runs SET claim_expires = 99, last_heartbeat_at = 98 WHERE task_id = ?", (tid,))
    changed, cursor = tc.changed_since(board, cursor)
    assert changed == frozenset()
    assert board.execute("SELECT revision FROM kanban_activity_revision").fetchone()[0] == before
    _write(board, "UPDATE task_runs SET status = 'crashed' WHERE task_id = ?", (tid,))  # content: marks
    assert tc.changed_since(board, cursor)[0] == {tid}


def test_an_unmarked_bump_is_never_trusted(board):
    """R7 (a): an older writer's triggers bump the counter without marking; MAX(rev) then trails the revision."""
    tid = kb.create_task(board, title="a", assignee="w")
    cursor = _armed(board)
    _write(board, "UPDATE kanban_activity_revision SET revision = revision + 1 WHERE id = 1")
    assert tc.changed_since(board, cursor) == (FULL, None)
    assert tc.changed_since(board, None) == (FULL, None)
    _write(board, "UPDATE tasks SET title = 'x' WHERE id = ?", (tid,))  # a marked bump re-aligns MAX(rev)
    first, fresh = tc.changed_since(board, None)
    assert first is FULL and fresh is not None


def _old_writer_init(conn) -> None:
    """What a writer from before P2 does on init: its bump-only triggers replace ours (and it bumps)."""
    found = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name GLOB ?",
                              (rev.PREFIX + "*",)))
    with kbc.write_txn(conn):
        conn.execute(rev._BUMP)
        for name, sql in found.items():
            head = sql.split(" BEGIN ", 1)[0]
            conn.execute(f"DROP TRIGGER {name}")
            conn.execute(f"{head} BEGIN {rev._BUMP} END")


def test_an_older_writer_dropping_the_marks_reads_full_until_reinstalled(board):
    tid = kb.create_task(board, title="a", assignee="w")
    cursor = _armed(board)
    _old_writer_init(board)
    _write(board, "UPDATE tasks SET title = 'unmarked' WHERE id = ?", (tid,))
    assert tc.changed_since(board, cursor) == (FULL, None)
    assert tc.changed_since(board, None) == (FULL, None)
    rev.ensure_revision(board, rev.LOCAL_TABLES)  # a current writer's init
    changed, fresh = tc.changed_since(board, cursor)
    assert changed is FULL and fresh is not None  # the gap's writes are unknown: one full read
    assert tc.changed_since(board, fresh)[0] == frozenset()


def test_an_older_writer_trigger_is_not_coverage_even_before_it_bumps(board):
    cursor = _armed(board)
    sql = board.execute("SELECT sql FROM sqlite_master WHERE name = ?",
                        (f"{rev.PREFIX}task_comments_insert",)).fetchone()[0]
    with kbc.write_txn(board):  # no bump: only the trigger's epoch tells
        board.execute(f"DROP TRIGGER {rev.PREFIX}task_comments_insert")
        board.execute(sql.split(" BEGIN ", 1)[0] + f" BEGIN {rev._BUMP} END")
    assert tc.changed_since(board, cursor) == (FULL, None)


def test_a_dropped_trigger_is_never_trusted(board):
    tid = kb.create_task(board, title="a", assignee="w")
    cursor = _armed(board)
    with kbc.write_txn(board):
        board.execute(f"DROP TRIGGER {rev.PREFIX}tasks_update")
    _write(board, "UPDATE tasks SET title = 'silent' WHERE id = ?", (tid,))
    assert tc.changed_since(board, cursor) == (FULL, None)


def test_a_database_restored_in_place_reads_full(board, tmp_path):
    """R7 (b)/(c): the backup API restores into the same inode; its counter regressed and schema_version moved."""
    tid = kb.create_task(board, title="a", assignee="w")
    snapshot = sqlite3.connect(tmp_path / "snap.db")
    board.backup(snapshot)
    for i in range(3):
        _write(board, "UPDATE tasks SET title = ? WHERE id = ?", (f"later {i}", tid))
    cursor = _armed(board)
    snapshot.backup(board)
    snapshot.close()
    assert tc.changed_since(board, cursor)[0] is FULL


def test_a_regressed_revision_alone_reads_full(board):
    """R7 (b) in isolation: same file, same schema, MAX(rev) == revision, but below the cursor."""
    tid = kb.create_task(board, title="a", assignee="w")
    low = board.execute("SELECT revision FROM kanban_activity_revision").fetchone()[0]
    for i in range(3):
        _write(board, "UPDATE tasks SET title = ? WHERE id = ?", (f"later {i}", tid))
    cursor = _armed(board)
    with kbc.write_txn(board):  # rewind both tables the way an older copy of the file reads
        board.execute("UPDATE kanban_activity_revision SET revision = ?", (low,))
        board.execute("UPDATE kanban_task_change SET rev = ? WHERE rev > ?", (low, low))
    assert tc.changed_since(board, cursor)[0] is FULL


def test_a_changed_shm_identity_alone_reads_full_and_rearms(board, monkeypatch):
    """A new WAL generation invalidates even an otherwise unchanged database cursor."""
    from hermes_cli import kanban_lockguard

    kb.create_task(board, title="a", assignee="w")
    cursor = _armed(board)
    path = tc._main_path(board)
    assert path is not None
    # Model the OS observation, not a live shm unlink (unsafe with SQLite open).
    replacement_shm = ("replacement-wal-generation",)
    assert cursor.shm != replacement_shm
    monkeypatch.setattr(kanban_lockguard, "shm_identity", lambda _path: replacement_shm)
    assert tc._main_path(board) == path
    assert tc._identity(path) == (cursor.dev, cursor.ino, replacement_shm)
    assert board.execute("PRAGMA schema_version").fetchone()[0] == cursor.schema_version
    assert board.execute("SELECT revision FROM kanban_activity_revision").fetchone()[0] == cursor.revision

    changed, fresh = tc.changed_since(board, cursor)
    assert changed is FULL
    assert fresh == tc.Cursor(cursor.dev, cursor.ino, replacement_shm,
                              cursor.schema_version, cursor.revision, cursor.tables)
    assert fresh != cursor
    assert tc.changed_since(board, fresh) == (frozenset(), fresh)


def test_a_replaced_file_reads_full(home, board, tmp_path):
    kb.create_task(board, title="a", assignee="w")
    cursor = _armed(board)
    other = kbc.connect(tmp_path / "other.db")
    try:
        kb.create_task(other, title="b", assignee="w")
        _armed(other)
        assert tc.changed_since(other, cursor)[0] is FULL  # same revision space, another file
    finally:
        other.close()


def test_ensure_revision_keeps_a_newer_epochs_triggers(board):
    """A newer writer's triggers (and its extra ones) survive this writer's init instead of being dropped."""
    names = dict(board.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name GLOB ?",
                               (rev.PREFIX + "*",)))
    newer = names[f"{rev.PREFIX}tasks_update"].replace(rev.EPOCH_MARK, "/* kanban-activity-epoch:3 */")
    extra = (f"CREATE TRIGGER {rev.PREFIX}future_table_insert AFTER INSERT ON tasks "
             f"BEGIN /* kanban-activity-epoch:3 */ {rev._BUMP} END")
    with kbc.write_txn(board):
        board.execute(f"DROP TRIGGER {rev.PREFIX}tasks_update")
        board.execute(newer)
        board.execute(extra)
    before = dict(board.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name GLOB ?",
                                (rev.PREFIX + "*",)))
    revision = board.execute("SELECT revision FROM kanban_activity_revision").fetchone()[0]
    rev.ensure_revision(board, rev.LOCAL_TABLES)
    after = dict(board.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name GLOB ?",
                               (rev.PREFIX + "*",)))
    assert after == before
    assert board.execute("SELECT revision FROM kanban_activity_revision").fetchone()[0] == revision  # no write


def test_ensure_revision_replaces_an_older_epochs_triggers(board):
    _old_writer_init(board)
    rev.ensure_revision(board, rev.LOCAL_TABLES)
    sqls = [r[0] for r in board.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name GLOB ?",
                                        (rev.PREFIX + "*",))]
    assert sqls and all(rev.trigger_epoch(s) == rev.EPOCH for s in sqls)


def test_shadow_runs_and_events_are_covered(shadow):
    """R8: a sync shadow marks task_runs/task_events (with the same exclusions), not only tasks/chat links."""
    assert {"task_runs", "task_events", "task_links", "task_comments"} <= set(rev.SHADOW_TABLES)
    with kbc.write_txn(shadow):
        shadow.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_hub', 'hub card', 'running', 1)")
    cursor = _armed(shadow)
    _write(shadow, "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES ('t_hub', 'w', 'running', 1)")
    changed, cursor = tc.changed_since(shadow, cursor)
    assert changed == {"t_hub"}
    _write(shadow, "INSERT INTO task_events (task_id, kind, created_at) VALUES ('t_hub', 'claimed', 2)")
    changed, cursor = tc.changed_since(shadow, cursor)
    assert changed == {"t_hub"}
    _write(shadow, "INSERT INTO task_events (task_id, kind, created_at) VALUES ('t_hub', 'heartbeat', 3)")
    _write(shadow, "UPDATE task_runs SET last_heartbeat_at = 3, claim_expires = 9 WHERE task_id = 't_hub'")
    changed, cursor = tc.changed_since(shadow, cursor)
    assert changed == frozenset()
    _write(shadow, "UPDATE task_runs SET status = 'done', ended_at = 4 WHERE task_id = 't_hub'")
    changed, cursor = tc.changed_since(shadow, cursor)
    assert changed == {"t_hub"}
    _write(shadow, "DELETE FROM tasks WHERE id = 't_hub'")  # a parent vanishing from the replica (R8 parked scope)
    assert tc.changed_since(shadow, cursor)[0] == {"t_hub"}


def test_a_trigger_dropped_and_recreated_around_a_write_reads_full(board):
    """(c) schema_version: the triggers look whole again and the counter never moved, but a write went unrecorded."""
    tid = kb.create_task(board, title="a", assignee="w")
    cursor = _armed(board)
    name = f"{rev.PREFIX}tasks_update"
    sql = board.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0]
    with kbc.write_txn(board):
        board.execute(f"DROP TRIGGER {name}")
        board.execute("UPDATE tasks SET title = 'unrecorded' WHERE id = ?", (tid,))
        board.execute(sql)
    assert tc.changed_since(board, cursor)[0] is FULL


def test_a_cursor_of_another_inode_reads_full(board):
    """(c) dev/ino, alone: the same revision space and schema, a cursor taken on another inode."""
    import dataclasses
    kb.create_task(board, title="a", assignee="w")
    cursor = _armed(board)
    assert tc.changed_since(board, dataclasses.replace(cursor, ino=cursor.ino + 1))[0] is FULL
    assert tc.changed_since(board, dataclasses.replace(cursor, dev=cursor.dev + 1))[0] is FULL
    assert tc.changed_since(board, cursor)[0] == frozenset()


def test_a_file_swapped_while_reading_is_not_vouched_for(board, monkeypatch):
    kb.create_task(board, title="a", assignee="w")
    cursor = _armed(board)
    real = tc._identity
    calls = []

    def moving(path):
        calls.append(path)
        found = real(path)
        return found if len(calls) == 1 else (found[0], found[1] + 1, found[2])

    monkeypatch.setattr(tc, "_identity", moving)
    assert tc.changed_since(board, cursor) == (FULL, None)


def test_an_unattributable_bump_reads_full(board):
    """A bump marked ``FULL_MARK`` (no task key to name) is explained, so (a) holds, but it names no task."""
    tid = kb.create_task(board, title="a", assignee="w")
    cursor = _armed(board)
    with kbc.write_txn(board):
        board.execute(rev._BUMP)
        board.execute(rev._mark("NULL"))
    changed, fresh = tc.changed_since(board, cursor)
    assert changed is FULL and fresh is not None
    _write(board, "UPDATE tasks SET title = 'after' WHERE id = ?", (tid,))
    assert tc.changed_since(board, fresh)[0] == {tid}


def test_a_missing_change_table_reads_full_and_init_repairs_it(board):
    tid = kb.create_task(board, title="a", assignee="w")
    cursor = _armed(board)
    with kbc.write_txn(board):
        board.execute(f"DROP TABLE {rev.CHANGE_TABLE}")
    assert tc.changed_since(board, cursor) == (FULL, None)
    rev.ensure_revision(board, rev.LOCAL_TABLES)
    _write(board, "UPDATE tasks SET title = 'after' WHERE id = ?", (tid,))
    first, fresh = tc.changed_since(board, cursor)
    assert first is FULL and fresh is not None


def test_the_change_table_has_a_rowid_like_every_board_table(board, shadow):
    """Generic every-table readers (``SELECT * FROM t ORDER BY rowid`` snapshots in the remote-lease tests, recovery
    scans) must keep working on boards and shadows once the change table exists."""
    tid = kb.create_task(board, title="a", assignee="w")
    for conn in (board, shadow):
        rows = conn.execute(f"SELECT rowid, task_id FROM {rev.CHANGE_TABLE} ORDER BY rowid").fetchall()
        assert conn is shadow or tid in [r[1] for r in rows]


def test_ensure_revision_repairs_a_dropped_change_index(board):
    with kbc.write_txn(board):
        board.execute(f"DROP INDEX {rev.CHANGE_INDEX}")
    rev.ensure_revision(board, rev.LOCAL_TABLES)
    assert board.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                         (rev.CHANGE_INDEX,)).fetchone()


def test_the_answer_is_read_in_one_snapshot(board):
    """Coverage, counter, MAX(rev) and the changed rows are read inside one read transaction."""
    kb.create_task(board, title="a", assignee="w")
    cursor = _armed(board)
    seen: list[str] = []
    board.set_trace_callback(seen.append)
    try:
        tc.changed_since(board, cursor)
    finally:
        board.set_trace_callback(None)
    begin = next(i for i, s in enumerate(seen) if s.strip().upper().startswith("BEGIN"))
    commit = max(i for i, s in enumerate(seen) if s.strip().upper().startswith("COMMIT"))
    reads = [i for i, s in enumerate(seen) if rev.CHANGE_TABLE in s or "schema_version" in s]
    assert reads and all(begin < i < commit for i in reads)


def test_a_cursor_inside_a_write_transaction_sees_its_own_writes(board):
    tid = kb.create_task(board, title="a", assignee="w")
    cursor = _armed(board)
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET title = 'mine' WHERE id = ?", (tid,))
        changed, _ = tc.changed_since(board, cursor)
    assert changed == {tid}


# [daveey] Review 5898003274 (B1/B2): a trigger's own conflict policy is overridden by the outer statement's. A mark
# written as ``INSERT OR REPLACE`` became ``OR IGNORE`` under ``INSERT OR IGNORE INTO task_links`` (the bump stayed, the
# mark was dropped: a non-FULL miss once a later write re-aligned MAX(rev)), and ``OR ABORT``/``FAIL``/``ROLLBACK`` made
# the content write raise UNIQUE on kanban_task_change. The mark is an ``ON CONFLICT DO UPDATE`` upsert, which no outer
# policy overrides.
def test_link_tasks_or_ignore_marks_the_already_marked_parent(board):
    parent = kb.create_task(board, title="parent", assignee="w")
    child = kb.create_task(board, title="child", assignee="w")
    other = kb.create_task(board, title="other", assignee="w")
    cursor = _armed(board)
    kb.link_tasks(board, parent, child)  # INSERT OR IGNORE INTO task_links (parent already has a mark row)
    _write(board, "UPDATE tasks SET title = 'later' WHERE id = ?", (other,))
    changed, _ = tc.changed_since(board, cursor)
    assert changed == {parent, child, other}


@pytest.mark.parametrize("policy", ["OR IGNORE", "OR ABORT", "OR FAIL", "OR ROLLBACK", "OR REPLACE", ""])
def test_every_outer_conflict_policy_marks_and_never_raises(board, policy):
    tid = kb.create_task(board, title="a", assignee="w")
    cursor = _armed(board)
    _write(board, f"INSERT {policy} INTO task_comments (task_id, author, body, created_at) VALUES (?, 'me', 'hi', 1)",
           (tid,))
    changed, _ = tc.changed_since(board, cursor)
    assert changed == {tid}


def test_update_or_ignore_marks_the_already_marked_task(board):
    tid = kb.create_task(board, title="a", assignee="w")
    _write(board, "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at) VALUES (?, 'p', 'c1', 1)",
           (tid,))
    cursor = _armed(board)
    _write(board, "UPDATE OR IGNORE kanban_notify_subs SET chat_id = 'c2' WHERE task_id = ?", (tid,))
    changed, _ = tc.changed_since(board, cursor)
    assert changed == {tid}
