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

# kanban_move_pool: a machine:<x> target needs a host_reason naming state on x (t_01a1d95c41620e6c).
HR = "the card's worktree is on that host"

svc = moves.svc
gws = moves.gws


@pytest.fixture
def moved_parent(gws, monkeypatch):
    hub, worker = gws
    hub.enter()
    task, _run, clock = helpers.park(hub.conn, monkeypatch)
    parent = kb.parent_ids(hub.conn, task)[0]
    clock[0] += 1
    assert km.move_queued(hub.conn, task, runs_on=["machine:worker-1"], host_reason=HR, reason="capacity",
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


MOVE_ONLY = {"missing_move", "queued_false", "checkpoint_list", "checkpoint_blank", "bad_move", "move_before_end"}


def strip_park_checkpoint(conn, task):
    """Leave only the move path: the park-scoped path has its own boundary tests below."""
    for event_id, payload in conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'dependency_wait'", (task,)).fetchall():
        data = json.loads(payload or "{}")
        data.pop("review_checkpoint", None)
        conn.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(data), event_id))


@pytest.mark.parametrize("case", [
    "local_parent", "not_parent", "missing_move", "queued_false", "checkpoint_list",
    "checkpoint_blank", "missing_run", "outcome", "unfinished", "bad_end", "bad_move",
    "move_before_end", "crashed", "reclaimed", "timed_out", "missing_table", "missing_row",
])
def test_reader_rejects_invalid_custody(moved_parent, monkeypatch, case):
    _, worker, task, parent = moved_parent
    conn = worker.conn
    original = kb.get_task
    if case in MOVE_ONLY:
        strip_park_checkpoint(conn, task)

    def change_payload(**updates):
        payload = {"queued": True, "checkpoint": "Resume", **updates}
        conn.execute("UPDATE task_events SET payload = ? WHERE task_id = ? AND kind = 'moved'",
                     (json.dumps(payload), task))

    def change_run(**updates):
        current = kanban_unused_grant.latest_started_run(conn, task)
        assert current is not None
        run = {**dict(current), **updates}
        monkeypatch.setattr(kanban_unused_grant, "latest_started_run", lambda *a: run)

    def change_move_time(value):
        conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'moved'",
                     (value, task))

    setup = {
        "local_parent": lambda: monkeypatch.setattr(kb, "get_task", lambda c, t: object() if t == parent else original(c, t)),
        "not_parent": lambda: monkeypatch.setattr(kb, "parent_states", lambda *a: []),
        "missing_move": lambda: conn.execute("DELETE FROM task_events WHERE task_id = ? AND kind = 'moved'", (task,)),
        "queued_false": lambda: change_payload(queued=False),
        "checkpoint_list": lambda: change_payload(checkpoint=["Resume"]),
        "checkpoint_blank": lambda: change_payload(checkpoint="   "),
        "missing_run": lambda: monkeypatch.setattr(kanban_unused_grant, "latest_started_run", lambda *a: None),
        "outcome": lambda: change_run(outcome="crashed"),
        "unfinished": lambda: change_run(ended_at=None),
        "bad_end": lambda: change_run(ended_at="invalid"),
        "bad_move": lambda: change_move_time("invalid"),
        "move_before_end": lambda: change_move_time(1),
        "crashed": lambda: kb._append_event(conn, task, "crashed", {}),
        "reclaimed": lambda: kb._append_event(conn, task, "reclaimed", {}),
        "timed_out": lambda: kb._append_event(conn, task, "timed_out", {}),
        "missing_table": lambda: conn.execute("DROP TABLE kanban_move_parent_results"),
        "missing_row": lambda: conn.execute("DELETE FROM kanban_move_parent_results"),
    }
    setup[case]()
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
    strip_park_checkpoint(worker.conn, task)
    payload = {"queued": True, "checkpoint": "Resume"}
    wanted = [] if case == "unwanted" else [parent]
    setup = {
        "queued_false": lambda: payload.update(queued=False),
        "checkpoint_list": lambda: payload.update(checkpoint=["Resume"]),
        "checkpoint_blank": lambda: payload.update(checkpoint="   "),
        "latest": lambda: payload.pop("checkpoint"),
        "done": lambda: worker.conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task,)),
        "archived": lambda: worker.conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (task,)),
        "unwanted": lambda: None,
    }
    setup[case]()
    kb._append_event(worker.conn, task, "moved", payload)
    values, changed = evidence.prepare(worker.conn, hub.conn, wanted)
    assert values == []
    assert changed == [parent]


@pytest.mark.parametrize("case", ["valid", "not_done", "not_claimed", "not_completed", "before_park", "missing_owner", "missing_evidence"])
def test_completed_review_requires_foreign_completion(moved_parent, monkeypatch, case):
    from hermes_cli import kanban_dependency_history
    from hermes_cli.kanban_dependency_review import _completed_review_run

    # Isolate the mirror's own rules: the worker's real, fresh replica would otherwise
    # supply the untampered parent run (test_kanban_dependency_review_replica_parent).
    monkeypatch.setattr(kanban_dependency_history, "replica_review_run", lambda *a: None)
    _, worker, task, parent = moved_parent
    owner = kanban_unused_grant.latest_started_run(worker.conn, task)
    assert owner is not None
    parked_at = kb._to_epoch(owner["ended_at"])
    assert parked_at is not None
    mirrored = evidence.result(worker.conn, task, parent)
    assert mirrored is not None
    assert mirrored["parent_status"] == "done"
    assert mirrored["was_claimed"]
    assert mirrored["outcome"] == "completed"
    mirrored["ended_at"] = parked_at
    overrides = {
        "not_done": {"parent_status": "ready"},
        "not_claimed": {"was_claimed": False},
        "not_completed": {"outcome": "crashed"},
        "before_park": {"ended_at": parked_at - 1},
    }
    mirrored.update(overrides.get(case, {}))
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


def _set_park(conn, task, mutate):
    event_id, payload = conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'dependency_wait' ORDER BY id DESC LIMIT 1",
        (task,)).fetchone()
    data = json.loads(payload)
    mutate(data)
    conn.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(data), event_id))


@pytest.mark.parametrize("case", [
    "valid", "uid_mismatch", "uid_missing_int_only", "parent_unnamed", "parents_not_list", "not_dependency",
    "older_run", "crashed",
])
def test_park_scoped_reader_without_move_checkpoint(moved_parent, case):
    """No checkpointed move: only the owner's own authenticated review park admits the mirror."""
    _, worker, task, parent = moved_parent
    conn = worker.conn
    conn.execute("DELETE FROM task_events WHERE task_id = ? AND kind = 'moved'", (task,))
    setup = {
        "valid": lambda: None,
        "uid_mismatch": lambda: _set_park(conn, task, lambda d: d["review_checkpoint"].update(run_uid="other-run")),
        # A missing uid falls back to the legacy integer, which on an importing file names another run.
        "uid_missing_int_only": lambda: _set_park(conn, task, lambda d: (
            d["review_checkpoint"].pop("run_uid"), d["review_checkpoint"].update(run_id=-5))),
        "parent_unnamed": lambda: _set_park(conn, task, lambda d: d["review_checkpoint"].update(parents=["t_other"])),
        "parents_not_list": lambda: _set_park(conn, task, lambda d: d["review_checkpoint"].update(parents=parent)),
        "not_dependency": lambda: _set_park(conn, task, lambda d: d.update(kind="needs_input")),
        "older_run": lambda: conn.execute(
            "UPDATE task_events SET run_id = -1 WHERE task_id = ? AND kind = 'dependency_wait'", (task,)),
        "crashed": lambda: kb._append_event(conn, task, "crashed", {}),
    }
    setup[case]()
    actual = evidence.result(conn, task, parent)
    assert (actual is not None) == (case == "valid")


def test_prepare_scopes_park_named_foreign_parents(moved_parent):
    hub, worker, task, parent = moved_parent
    worker.conn.execute("DELETE FROM task_events WHERE task_id = ? AND kind = 'moved'", (task,))
    assert [p for p, _ in evidence.prepare(worker.conn, hub.conn, [parent])[0]] == [parent]
    _set_park(worker.conn, task, lambda d: d["review_checkpoint"].update(run_uid="other-run"))
    assert evidence.prepare(worker.conn, hub.conn, [parent])[0] == []
