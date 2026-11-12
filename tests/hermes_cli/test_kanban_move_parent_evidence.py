"""Fail-closed boundaries of the move-scoped foreign-result reader."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_move as km
from hermes_cli import kanban_move_parent_evidence as evidence
from hermes_cli import kanban_unused_grant
from tests.hermes_cli import test_kanban_dependency_pr_handoff as helpers
from tests.hermes_cli import test_kanban_move as moves

svc = moves.svc
gws = moves.gws


@pytest.fixture
def moved_parent(gws, monkeypatch):
    hub, worker = gws
    hub.enter()
    task, _run, clock = helpers.park(hub.conn, monkeypatch)
    parent = kb.parent_ids(hub.conn, task)[0]
    clock[0] += 1
    assert km.move_queued(hub.conn, task, runs_on=["machine:worker-1"], reason="capacity",
                          checkpoint="Resume existing PR")["ok"]
    # Use the source's real evidence with an independent primary. No dispatch
    # claim consumes the checkpoint before individual reader boundaries run.
    from hermes_cli import kanban_db_dispatch_pr_state as prs
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    hub.round()
    worker.round()
    # A short review SHA deliberately holds target dispatch while importing it.
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE task_runs SET metadata = ? WHERE task_id = ?",
                         (json.dumps({"verdict": "approve", "reviewed_sha": "short", "pr": helpers.PR}), parent))
    hub.round()
    worker.round()
    assert (task, "active_pr") in worker.tick().respawn_guarded
    assert evidence.result(worker.conn, task, parent)
    return hub, worker, task, parent


@pytest.mark.parametrize("case", [
    "local_parent", "not_parent", "missing_move", "queued_false", "checkpoint_list",
    "checkpoint_blank", "missing_run", "outcome", "unfinished", "bad_end", "bad_move",
    "move_before_end", "crashed", "reclaimed", "timed_out", "missing_table", "missing_row",
])
def test_reader_rejects_invalid_custody(moved_parent, monkeypatch, case):
    _, worker, task, parent = moved_parent
    conn = worker.conn
    if case == "local_parent":
        original = kb.get_task
        monkeypatch.setattr(kb, "get_task", lambda c, t: object() if t == parent else original(c, t))
    elif case == "not_parent":
        monkeypatch.setattr(kb, "parent_states", lambda *a: [])
    elif case == "missing_move":
        conn.execute("DELETE FROM task_events WHERE task_id = ? AND kind = 'moved'", (task,))
    elif case in ("queued_false", "checkpoint_list", "checkpoint_blank"):
        payload = {"queued": True, "checkpoint": "Resume"}
        if case == "queued_false":
            payload["queued"] = False
        else:
            payload["checkpoint"] = ["Resume"] if case == "checkpoint_list" else "   "
        conn.execute("UPDATE task_events SET payload = ? WHERE task_id = ? AND kind = 'moved'",
                     (json.dumps(payload), task))
    elif case in ("missing_run", "outcome", "unfinished", "bad_end"):
        run = dict(kanban_unused_grant.latest_started_run(conn, task))
        if case == "outcome":
            run["outcome"] = "crashed"
        elif case == "unfinished":
            run["ended_at"] = None
        elif case == "bad_end":
            run["ended_at"] = "invalid"
        monkeypatch.setattr(kanban_unused_grant, "latest_started_run", lambda *a: None if case == "missing_run" else run)
    elif case in ("bad_move", "move_before_end"):
        conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'moved'",
                     ("invalid" if case == "bad_move" else 1, task))
    elif case in ("crashed", "reclaimed", "timed_out"):
        kb._append_event(conn, task, case, {})
    elif case == "missing_table":
        conn.execute("DROP TABLE kanban_move_parent_results")
    else:
        conn.execute("DELETE FROM kanban_move_parent_results")
    assert evidence.result(conn, task, parent) is None


def test_unchanged_sync_does_not_write_and_removed_parent_drops_evidence(moved_parent):
    hub, worker, task, parent = moved_parent
    sync.refresh_remote_parents(worker.conn, hub.conn)
    statements = []
    worker.conn.set_trace_callback(statements.append)
    try:
        assert sync.refresh_remote_parents(worker.conn, hub.conn) == []
    finally:
        worker.conn.set_trace_callback(None)
    assert not any(s.startswith(("BEGIN", "CREATE", "DELETE", "INSERT", "UPDATE")) for s in statements)
    hub.conn.execute("DELETE FROM task_runs WHERE task_id = ?", (parent,))
    assert sync.refresh_remote_parents(worker.conn, hub.conn) == [parent]
    assert evidence.result(worker.conn, task, parent) is None


@pytest.mark.parametrize("case", ["queued_false", "checkpoint_list", "checkpoint_blank", "latest", "done", "archived", "unwanted"])
def test_prepare_scopes_only_latest_active_checkpoint(moved_parent, case):
    hub, worker, task, parent = moved_parent
    assert evidence.prepare(worker.conn, hub.conn, [parent])[0]
    payload = {"queued": True, "checkpoint": "Resume"}
    wanted = [parent]
    if case == "queued_false":
        payload["queued"] = False
    elif case == "checkpoint_list":
        payload["checkpoint"] = ["Resume"]
    elif case == "checkpoint_blank":
        payload["checkpoint"] = "   "
    elif case == "latest":
        payload = {"queued": True}
    elif case in ("done", "archived"):
        worker.conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (case, task))
    else:
        wanted = []
    kb._append_event(worker.conn, task, "moved", payload)
    values, changed = evidence.prepare(worker.conn, hub.conn, wanted)
    assert values == []
    assert changed == [parent]


@pytest.mark.parametrize("case", ["valid", "not_done", "not_claimed", "not_completed", "before_park", "missing_owner", "missing_evidence"])
def test_completed_review_requires_foreign_completion(moved_parent, case):
    from hermes_cli.kanban_dependency_review import _completed_review_run

    _, worker, task, parent = moved_parent
    owner = kanban_unused_grant.latest_started_run(worker.conn, task)
    parked_at = kb._to_epoch(owner["ended_at"])
    mirrored = evidence.result(worker.conn, task, parent)
    assert mirrored["parent_status"] == "done"
    assert mirrored["was_claimed"]
    assert mirrored["outcome"] == "completed"
    mirrored["ended_at"] = parked_at
    if case == "not_done":
        mirrored["parent_status"] = "ready"
    elif case == "not_claimed":
        mirrored["was_claimed"] = False
    elif case == "not_completed":
        mirrored["outcome"] = "crashed"
    elif case == "before_park":
        mirrored["ended_at"] = parked_at - 1
    worker.conn.execute("UPDATE kanban_move_parent_results SET payload = ? WHERE parent_id = ?",
                        (json.dumps(mirrored), parent))
    if case == "missing_evidence":
        worker.conn.execute("DELETE FROM kanban_move_parent_results")
    owner_id = -1 if case == "missing_owner" else owner["id"]
    actual = _completed_review_run(worker.conn, parent, parked_at, owner_id)
    assert actual == (mirrored if case == "valid" else None)


def test_refresh_reads_status_and_result_in_one_snapshot(moved_parent, monkeypatch):
    import sqlite3

    hub, worker, task, parent = moved_parent
    path = hub.conn.execute("PRAGMA database_list").fetchone()[2]
    assert hub.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    original = evidence.prepare

    def change_between_reads(live, replica, wanted):
        other = sqlite3.connect(path, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
            other.execute("UPDATE task_runs SET outcome = 'crashed' WHERE task_id = ?", (parent,))
            other.commit()
        finally:
            other.close()
        return original(live, replica, wanted)

    monkeypatch.setattr(evidence, "prepare", change_between_reads)
    sync.refresh_remote_parents(worker.conn, hub.conn)
    assert dict(kb.parent_states(worker.conn, task))[parent] == "done"
    assert evidence.result(worker.conn, task, parent)["outcome"] == "completed"
    monkeypatch.setattr(evidence, "prepare", original)
    sync.refresh_remote_parents(worker.conn, hub.conn)
    assert dict(kb.parent_states(worker.conn, task))[parent] == "ready"
    assert evidence.result(worker.conn, task, parent)["outcome"] == "crashed"
