"""Where a listed card runs, for the Desktop strip's hub/worker mark (``kanban_run_machine``). [daveey]"""

import sqlite3

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_session_activity as ksa
from hermes_cli.kanban_run_machine import POOL, add_run_machines, run_machine


@pytest.mark.parametrize("row,want", [
    ({"status": "running", "lease_holder": "hub", "runs_on": '["machine:worker-1"]'}, "hub"),  # the lease wins
    ({"status": "running", "lease_holder": "worker-1", "created_on": "hub"}, "worker-1"),
    # A ready card's expired lease names where it last ran, not where it will: its pin does.
    ({"status": "ready", "lease_holder": "worker-1", "lease_expires": 1_000, "runs_on": '["machine:hub"]'}, "hub"),
    # An unexpired lease on a waiting card is the Board's assignment.
    ({"status": "ready", "lease_holder": "hub", "lease_expires": 4e12, "placement_policy": "worker-first-v1"}, "hub"),
    ({"status": "todo", "runs_on": '["machine:worker-1"]', "created_on": "hub"}, "worker-1"),
    ({"status": "ready", "runs_on": ["machine:hub"]}, "hub"),  # already decoded
    ({"status": "ready", "placement_policy": "worker-first-v1", "created_on": "hub"}, POOL),
    ({"status": "ready", "runs_on": '["linux"]', "created_on": "hub"}, None),  # any machine with the label
    ({"status": "ready", "runs_on": "[]", "created_on": "hub"}, None),  # explicit any machine
    ({"status": "ready", "runs_on": '["machine:a", "machine:b"]', "created_on": "hub"}, None),
    ({"status": "scheduled", "created_on": "macbook"}, "macbook"),  # unplaced: runs where it was created
    ({"status": "running", "created_on": "hub"}, "hub"),  # no sync lease: still its origin
    ({"status": "ready"}, None),
    ({"status": "ready", "runs_on": "{bad", "created_on": "hub"}, "hub"),  # undecodable pin reads as none
])
def test_run_machine(row, want):
    assert run_machine(row) == want


def test_add_run_machines_tolerates_old_boards():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tasks (id TEXT, status TEXT)")
    conn.execute("INSERT INTO tasks VALUES ('t1', 'ready')")
    found = {"t1": {"id": "t1", "status": "ready"}}
    add_run_machines(conn, found)
    assert "run_machine" not in found["t1"]


def _board(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL, assignee TEXT, session_id TEXT, "
        "created_at INTEGER, started_at INTEGER, runs_on TEXT, placement_policy TEXT, lease_holder TEXT, "
        "created_on TEXT)")
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, 'default', 's_me', 100, NULL, ?, ?, ?, 'hub')", [
        ("t_hub", "On hub", "running", '["machine:hub"]', None, "hub"),
        ("t_w", "On worker", "running", None, "worker-first-v1", "worker-1"),
        ("t_pool", "Pool wait", "ready", None, "worker-first-v1", None),
        ("t_pin", "Pinned", "todo", '["machine:worker-1"]', None, None),
        ("t_here", "Unplaced", "scheduled", None, None, None),
    ])
    conn.commit()
    conn.close()


def test_session_tasks_stamp_run_machine(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(ksa, "_kanban_root", lambda: root, raising=False)
    monkeypatch.setattr(ksa, "_remote_cards", lambda ids, statuses, stale_ok: [])
    _board(root / "kanban.db")
    monkeypatch.setattr(ksa, "_board_db_paths", lambda: [root / "kanban.db"])
    ksa._reset_cache_for_tests()
    got = {t["id"]: t.get("run_machine") for t in ksa.session_tasks(["s_me"])}
    assert got == {"t_hub": "hub", "t_w": "worker-1", "t_pool": POOL, "t_pin": "worker-1", "t_here": "hub"}


def test_replica_cards_stamp_run_machine(tmp_path, monkeypatch):
    rep = tmp_path / "default.db"
    conn = sqlite3.connect(rep)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, created_at INTEGER, "
        "started_at INTEGER, priority INTEGER, block_kind TEXT, human_request TEXT, created_on TEXT, runs_on TEXT, "
        "placement_policy TEXT, lease_holder TEXT);"
        "CREATE TABLE task_chat_links (uid TEXT, task_id TEXT, chat_gateway TEXT, chat_profile TEXT, "
        "chat_session TEXT, kind TEXT, released INTEGER, created_at INTEGER);")
    conn.executemany("INSERT INTO tasks VALUES (?, 't', ?, 'w', 1, NULL, 0, NULL, NULL, 'macbook', ?, NULL, ?)", [
        ("t_hub", "running", None, "hub"), ("t_mac", "ready", None, None)])
    conn.executemany("INSERT INTO task_chat_links VALUES (?, ?, 'hub', 'default', 's1', 'shepherd', 0, 1)",
                     [("u1", "t_hub"), ("u2", "t_mac")])
    conn.commit()
    conn.close()
    monkeypatch.setattr(links, "_ledger_owners", lambda stale_ok=False: {})
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    got = {c["id"]: c.get("run_machine") for c in links.replica_cards(None, "hub", ksa.LISTED_STATUSES,
                                                                      replicas=[("default", rep)])}
    assert got == {"t_hub": "hub", "t_mac": "macbook"}
