"""Notification custody is the row's gateway, not its presence or body's host."""
import sqlite3
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_capability_routing as kcr
from hermes_cli import kanban_db as kb
from hermes_cli import update_pause_siblings as sib


@pytest.fixture
def boards(monkeypatch):
    connections = []

    def make(owner_column=True):
        conn = sqlite3.connect(":memory:")
        connections.append(conn)
        conn.execute("CREATE TABLE tasks (id TEXT, body TEXT, created_at INTEGER, created_by TEXT, title TEXT, status TEXT" +
                     (", created_on TEXT)" if owner_column else ")"))
        conn.execute("CREATE TABLE task_comments (task_id TEXT, author TEXT, body TEXT)")
        return conn

    @contextmanager
    def no_replica(conn):
        yield None

    def comment(conn, task_id, author, body):
        conn.execute("INSERT INTO task_comments VALUES (?, ?, ?)", (task_id, author, body))

    monkeypatch.setattr(sib, "_replica", no_replica)
    monkeypatch.setattr(kb, "add_comment", comment)
    yield make
    for conn in connections:
        conn.close()


def alarm(conn, task_id, host, owner=None, sibling=None):
    body = f"On `{host}`\nLatest refusal: busy\n"
    if sibling:
        body += f"Sibling alarm: {sibling}\n"
    conn.execute("INSERT INTO tasks (id, body, created_at, created_by, title, status) VALUES (?, ?, 1, ?, ?, 'ready')",
                 (task_id, body, sib.AUTHOR, sib.TITLE + "gateway"))
    if "created_on" in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}:
        conn.execute("UPDATE tasks SET created_on = ? WHERE id = ?", (owner, task_id))


@pytest.mark.parametrize("gateway,owner", [("worker-1", "worker-5"), (None, "worker-5")])
def test_foreign_pickup_skips_direct_write_and_home_mirrors_once(boards, monkeypatch, gateway, owner):
    pickup, home = boards(), boards()
    for conn in (pickup, home):
        alarm(conn, "t_leader", "shared-hostname", owner)
    alarm(pickup, "t_follower", "worker-1", "worker-1", "t_leader")
    monkeypatch.setattr(kcr, "_own_gateway", lambda conn: gateway)
    monkeypatch.setattr(sib.socket, "gethostname", lambda: "shared-hostname")
    sibling = sib._board_alarms(pickup, "gateway")[1]
    assert sibling["id"] == "t_leader"
    assert not sib.notify(pickup, sibling, "t_follower", "worker-1", "now", "busy")
    assert pickup.execute("SELECT * FROM task_comments").fetchall() == []

    @contextmanager
    def replica(conn):
        yield pickup

    monkeypatch.setattr(sib, "_replica", replica)
    monkeypatch.setattr(kcr, "_own_gateway", lambda conn: owner)
    assert sib.mirror_followers(home, "t_leader", "gateway", frozenset({owner})) == ["t_follower"]
    assert sib.mirror_followers(home, "t_leader", "gateway", frozenset({owner})) == []
    assert home.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0] == 1


@pytest.mark.parametrize("column,owner,gateway", [
    (True, "worker-1", "worker-1"),
    (True, None, "worker-1"),
    (True, "", "worker-1"),
    (False, None, None),
])
def test_owned_and_legacy_same_board_notifications(boards, monkeypatch, column, owner, gateway):
    conn = boards(column)
    alarm(conn, "t_leader", "another-host", owner)
    monkeypatch.setattr(kcr, "_own_gateway", lambda conn: gateway)
    sibling = sib._board_alarms(conn, "gateway")[0]
    assert sib.notify(conn, sibling, "t_follower", "local-host", "now", "busy")
    assert not sib.notify(conn, sibling, "t_follower", "local-host", "now", "busy")
    assert conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0] == 1
