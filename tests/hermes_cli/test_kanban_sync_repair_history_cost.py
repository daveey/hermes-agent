"""Repair work must scale with relevant links, not historical event deletions."""
import sqlite3
from contextlib import closing

import pytest

from hermes_cli import kanban_db_sync, kanban_placement
from hermes_cli import kanban_sync_client as client


def _db():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tasks(id TEXT PRIMARY KEY, sync_version INTEGER, created_on TEXT);
        CREATE TABLE task_links(uid TEXT, parent_id TEXT, child_id TEXT);
        CREATE TABLE task_chat_links(uid TEXT, task_id TEXT, chat_session TEXT, chat_gateway TEXT);
        CREATE TABLE kanban_sync_tombstones(table_name TEXT, row_uid TEXT, deleted_at INTEGER,
                                           PRIMARY KEY(table_name, row_uid));
        CREATE TABLE kanban_sync_outbox(op_id TEXT, table_name TEXT, row_uid TEXT, op TEXT,
            fields TEXT, base_version INTEGER, hlc TEXT, created_at INTEGER, state TEXT DEFAULT 'pending');
        INSERT INTO tasks VALUES ('parent', 1, 'macbook'), ('child', 1, 'macbook');
    """)
    conn.executescript(kanban_db_sync.SYNC_SCHEMA_SQL)
    return conn


def _run(live, shadow, retirement):
    if retirement:
        return client.retire_lost_placeholder_links(live, shadow, "macbook")
    return client.repair_missing_children(live, shadow, "macbook")


@pytest.mark.parametrize("retirement", [False, True])
@pytest.mark.parametrize("history_side", ["live", "shadow"])
def test_unrelated_tombstone_history_has_bounded_vm_work(monkeypatch, retirement, history_side):
    monkeypatch.setattr(kanban_placement, "config", dict)
    with closing(_db()) as live, closing(_db()) as shadow:
        shadow.execute("INSERT INTO task_chat_links VALUES ('gone', 'child', 'kanban-placement-macbook', 'macbook')")
        history = live if history_side == "live" else shadow
        history.executemany("INSERT INTO kanban_sync_tombstones VALUES ('task_events', ?, 1)",
                            [(str(i),) for i in range(10000)])
        ticks = []
        for conn in (live, shadow):
            conn.set_progress_handler(lambda: ticks.append(1) or 0, 1000)
        _run(live, shadow, retirement)
        for conn in (live, shadow):
            conn.set_progress_handler(None, 0)
        assert len(ticks) < 10, f"repair scanned unrelated history: {len(ticks)}k VM steps"
        if retirement:
            assert [tuple(r) for r in live.execute("SELECT op, row_uid FROM kanban_sync_outbox")] == [("delete", "gone")]


@pytest.mark.parametrize("side", ["live", "shadow"])
@pytest.mark.parametrize("table,uid", [("tasks", "child"), ("task_links", "parent>child"),
                                      ("task_chat_links", "chat")])
def test_relevant_tombstones_still_veto_missing_child_repair(side, table, uid):
    with closing(_db()) as live, closing(_db()) as shadow:
        live.execute("INSERT INTO task_links VALUES ('parent>child', 'parent', 'child')")
        live.execute("INSERT INTO task_chat_links VALUES ('chat', 'child', 'real-chat', 'macbook')")
        tombstones = live if side == "live" else shadow
        tombstones.execute("INSERT INTO kanban_sync_tombstones VALUES (?, ?, 1)", (table, uid))
        client.repair_missing_children(live, shadow, "macbook")
        queued = {r[0] for r in live.execute("SELECT row_uid FROM kanban_sync_outbox")}
        expected = set() if table == "tasks" else {"parent>child", "chat"} - {uid}
        assert queued == expected


def test_retirement_preserves_tombstone_and_inflight_vetoes(monkeypatch):
    monkeypatch.setattr(kanban_placement, "config", dict)
    with closing(_db()) as live, closing(_db()) as shadow:
        for uid in ("deleted", "pending", "rejected", "free"):
            shadow.execute("INSERT INTO task_chat_links VALUES (?, 'child', 'kanban-placement-macbook', 'macbook')", (uid,))
        live.execute("INSERT INTO kanban_sync_tombstones VALUES ('task_chat_links', 'deleted', 1)")
        live.execute("INSERT INTO kanban_sync_tombstones VALUES ('task_events', 'free', 1)")
        for state in ("pending", "rejected"):
            live.execute("INSERT INTO kanban_sync_outbox(table_name, row_uid, state) VALUES ('task_chat_links', ?, ?)",
                         (state, state))
        assert client.retire_lost_placeholder_links(live, shadow, "macbook") == 1
        assert [r[0] for r in live.execute("SELECT row_uid FROM kanban_sync_outbox WHERE op = 'delete'")] == ["free"]
