"""Actual client recovery writers, returning (storage table, SQLite rowid).

Only domain rows are seeded directly. An exact shadow backup and suppressed
domain capture model missed writes; real recovery functions insert the outbox.
"""
from contextlib import contextmanager
import sqlite3

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client

GATEWAY = "macbook"
OUTBOX = "kanban_sync_outbox"



@contextmanager
def _gap(conn):
    """Suppress normal domain capture while staging a recovery scenario."""
    with kbc.write_txn(conn):
        previous = conn.execute("SELECT applying FROM kanban_sync_state").fetchone()[0]
        conn.execute("UPDATE kanban_sync_state SET applying=1")
        try:
            yield
        finally:
            conn.execute("UPDATE kanban_sync_state SET applying=?", (previous,))


def _tasks(board, count=1):
    client.ensure_identity(board, "default", GATEWAY)
    # Fixture-only domain records represent already acknowledged local tasks.
    # Clearing their captured operations is preparation, NOT the insertion under test.
    tids = [kb.create_task(board, title=f"gate2 recovery {i}", assignee="worker",
                           initial_status="running") for i in range(count)]
    with _gap(board):
        for tid in tids:
            board.execute("UPDATE tasks SET sync_version=7,created_on=? WHERE id=?", (GATEWAY, tid))
    board.execute("DELETE FROM kanban_sync_outbox")
    return tids


@contextmanager
def _shadow(board):
    assert not board.in_transaction, "backup requires committed fixture preparation"
    shadow = sqlite3.connect(":memory:", isolation_level=None)
    shadow.row_factory = sqlite3.Row
    try:
        board.backup(shadow)
        yield shadow
    finally:
        shadow.close()


def _before(board):
    return {row[0] for row in board.execute(f"SELECT rowid FROM {OUTBOX}")}


def _inserted(board, before, expected):
    rows = [row for row in board.execute(
        f"SELECT rowid,table_name,row_uid,op FROM {OUTBOX} ORDER BY rowid") if row[0] not in before]
    assert [(row[1], row[2], row[3]) for row in rows] == expected, [tuple(r) for r in rows]
    return [(OUTBOX, row[0]) for row in rows]


def case_import_board(board, monkeypatch):
    """client.import_board's explicit recovery/import INSERT (client.py:1658)."""
    (tid,) = _tasks(board)
    before = _before(board)
    counts = client.import_board(board, tables=("tasks",), only={"tasks": {tid}})
    assert counts["tasks"] == 1
    return _inserted(board, before, [("tasks", tid, "import")])


def case_repair_missing_children(board, monkeypatch):
    """client.repair_missing_children's INSERT, for BOTH supported child types."""
    parent, child = _tasks(board, 2)
    chat_uid = "gate2-gap-chat"
    with _shadow(board) as shadow:
        with _gap(board):
            board.execute("INSERT INTO task_links(parent_id,child_id) VALUES (?,?)", (parent, child))
            board.execute("INSERT INTO task_chat_links"
                          "(uid,task_id,chat_gateway,chat_session,kind,created_at) "
                          "VALUES (?,?,?,'gate2-real-session','created',1)", (chat_uid, child, GATEWAY))
        before = _before(board)
        client.repair_missing_children(board, shadow, GATEWAY)
    return _inserted(board, before, [
        ("task_links", f"{parent}>{child}", "upsert"),
        ("task_chat_links", chat_uid, "upsert"),
    ])


def case_retire_lost_placeholder_links(board, monkeypatch):
    """client.retire_lost_placeholder_links's DELETE-operation outbox INSERT."""
    from hermes_cli import kanban_placement
    # Match existing retirement tests; no environment-specific placement prefixes.
    monkeypatch.setattr(kanban_placement, "config", dict)
    (tid,) = _tasks(board)
    uid = "gate2-lost-placeholder"
    with _gap(board):
        board.execute("INSERT INTO task_chat_links"
                      "(uid,task_id,chat_gateway,chat_session,kind,created_at) "
                      "VALUES (?,?,?,'kanban-placement-macbook','created',1)", (uid, tid, GATEWAY))
    with _shadow(board) as shadow:
        with _gap(board):
            board.execute("DELETE FROM task_chat_links WHERE uid=?", (uid,))
        # applying suppresses the ordinary delete capture and local tombstone.
        assert board.execute("SELECT 1 FROM kanban_sync_tombstones "
                             "WHERE table_name='task_chat_links' AND row_uid=?", (uid,)).fetchone() is None
        before = _before(board)
        assert client.retire_lost_placeholder_links(board, shadow, GATEWAY) == 1
    assert board.execute("SELECT 1 FROM kanban_sync_tombstones "
                         "WHERE table_name='task_chat_links' AND row_uid=?", (uid,)).fetchone()
    return _inserted(board, before, [("task_chat_links", uid, "delete")])


def case_repair_divergence_task(board, monkeypatch):
    """Real client wrapper -> divergence.repair_divergence task INSERT (:86)."""
    (tid,) = _tasks(board)
    with _shadow(board) as shadow:
        with _gap(board):
            board.execute("UPDATE tasks SET title='gate2 uncaptured title' WHERE id=?", (tid,))
        before = _before(board)
        assert client.repair_divergence(board, shadow, GATEWAY) == {tid: ["title"]}
    return _inserted(board, before, [("tasks", tid, "upsert")])


def case_repair_divergence_run(board, monkeypatch):
    """Real wrapper -> divergence run INSERT (:81) then task INSERT (:86)."""
    (tid,) = _tasks(board)
    uid = "gate2-diverged-run"
    with _gap(board):
        board.execute("INSERT INTO task_runs(uid,task_id,status,started_at,sync_version,summary) "
                      "VALUES (?,?,'done',1,7,'service summary')", (uid, tid))
    with _shadow(board) as shadow:
        with _gap(board):
            # A task difference is required: _diverged_runs is reached only for a
            # task with actual unsent fields. Use title to avoid lifecycle adoption.
            board.execute("UPDATE tasks SET title='gate2 uncaptured run task' WHERE id=?", (tid,))
            board.execute("UPDATE task_runs SET summary='uncaptured summary' WHERE uid=?", (uid,))
        before = _before(board)
        assert client.repair_divergence(board, shadow, GATEWAY) == {tid: ["title", f"task_runs:{uid}"]}
    return _inserted(board, before, [("task_runs", uid, "upsert"), ("tasks", tid, "upsert")])
