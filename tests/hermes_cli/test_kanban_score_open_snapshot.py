"""The admission snapshot reads open rows, not whole tables, and keeps the full-table answers.

A dispatcher pass ranks every lane, and each rank read every task and edge row of the hub DB and the replica. In the
hub gateway (hundreds of threads) each fetched row re-takes the GIL, so one pass took 30+ minutes on 2026-11-23 and
ready cards got no claim and no skip reason. These tests pin the bounded read and that the scores do not change.
"""
import sqlite3

import pytest

from hermes_cli import kanban_admission_score as score


def _database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tasks (id TEXT PRIMARY KEY, priority INTEGER, status TEXT, created_at INTEGER,
                            admission_class TEXT, consecutive_failures INTEGER);
        CREATE TABLE task_events (task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER);
        CREATE TABLE task_runs (task_id TEXT, ended_at INTEGER);
        CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
    """)
    return conn


@pytest.fixture
def sources(monkeypatch):
    from hermes_cli import kanban_db_sync, kanban_worker_node

    local, remote = _database(), _database()

    class Reader:
        def execute(self, *args):
            return remote.execute(*args)

        def close(self):
            pass

    monkeypatch.setattr(kanban_db_sync, "board_mode", lambda conn: "primary")
    monkeypatch.setattr(kanban_worker_node, "open_replica", lambda conn: Reader())
    monkeypatch.setattr(score, "_apply_turn_floor", lambda conn, tasks, now_s=None: frozenset())
    yield local, remote
    local.close()
    remote.close()


def add(conn, tid, status="ready", priority=0, created=1000):
    conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, 'normal', 0)", (tid, priority, status, created))


def link(conn, parent, child):
    conn.execute("INSERT INTO task_links VALUES (?, ?)", (parent, child))


def test_snapshot_skips_closed_history(sources):
    local, _ = sources
    for i in range(3000):
        add(local, f"done-{i}", "done", priority=1000)
        link(local, f"done-{i}", f"done-{(i + 1) % 3000}")
    for i in range(500):
        add(local, f"arch-{i}", "archived", priority=1000)
    add(local, "cand")
    add(local, "open-child", "todo", priority=700)
    link(local, "cand", "open-child")
    link(local, "cand", "done-7")

    tasks, edges, _meta, _since = score._source_snapshot(local, ["cand"])
    assert sorted(t[0] for t in tasks) == ["cand", "open-child"]
    assert [tuple(e) for e in edges] == [("cand", "open-child")]

    (_, parts), = score.rank(local, [{"id": "cand"}], now_ms=2_000_000, weights={})
    assert (parts.effective_priority, parts.descendants) == (700, 1)


def test_named_rows_are_read_whatever_their_status(sources):
    local, _ = sources
    add(local, "finished", "done", priority=400)
    add(local, "gone", "archived", priority=300)
    tasks, _edges, _meta, _since = score._source_snapshot(local, ["finished"], include_ids=["gone"])
    assert sorted(t[0] for t in tasks) == ["finished", "gone"]
    # effective_priorities still answers for closed ids it is asked about, as the full read did.
    assert score.effective_priorities(local, ["finished", "gone", "missing"]) == {"finished": 400, "gone": 300}


def test_local_done_row_still_overrides_open_replica_row(sources):
    local, remote = sources
    add(local, "cand")
    add(remote, "child", "todo", priority=900)
    link(remote, "cand", "child")
    (_, open_parts), = score.rank(local, [{"id": "cand"}], now_ms=2_000_000, weights={})
    assert (open_parts.effective_priority, open_parts.descendants) == (900, 1)

    add(local, "child", "done", priority=900)  # local custody: the child finished here
    (_, parts), = score.rank(local, [{"id": "cand"}], now_ms=2_000_000, weights={})
    assert (parts.effective_priority, parts.descendants) == (0, 0)


def test_edge_to_child_known_only_to_replica_still_counts(sources):
    local, remote = sources
    add(local, "cand")
    link(local, "cand", "remote-child")  # child row never reached the hub DB
    add(remote, "remote-child", "ready", priority=800)
    (_, parts), = score.rank(local, [{"id": "cand"}], now_ms=2_000_000, weights={})
    assert (parts.effective_priority, parts.descendants) == (800, 1)


def test_more_named_ids_than_one_batch(sources):
    local, remote = sources
    add(local, "cand")
    for i in range(950):
        add(remote, f"r{i}", "todo", priority=i)
        add(local, f"r{i}", "done", priority=i)
        link(remote, "cand", f"r{i}")
    # Every open replica row is closed locally: all 950 custody lookups must land (3 batches).
    (_, parts), = score.rank(local, [{"id": "cand"}], now_ms=2_000_000, weights={})
    assert (parts.effective_priority, parts.descendants) == (0, 0)
