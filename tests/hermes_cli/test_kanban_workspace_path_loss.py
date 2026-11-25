"""Host-local source bindings must outlive eviction and never spend spawn retries."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_workspace as workspace
from hermes_cli import kanban_worker_node as worker
from tests.hermes_cli import test_kanban_worker_node as pickup
from tests.hermes_cli import test_kanban_worktree_create_validation as validation

svc = pickup.svc
gws = pickup.gws
kanban_home = validation.kanban_home


@pytest.mark.parametrize("kind", ["dir", "worktree"])
def test_evict_reimport_preserves_local_source_binding(gws, tmp_path, kind):
    hub, node = gws
    path = tmp_path / "bound-source"
    path.mkdir()
    hub.enter()
    tid = kb.create_task(hub.conn, title="source bound by bridge", assignee="default",
                         workspace_kind=kind, workspace_path=str(path),
                         runs_on=[f"machine:{pickup.WORKER}"])
    hub.round()
    node.round()
    node.enter()
    replica = worker.open_replica(node.conn)
    try:
        assert worker.import_cards(node.conn, replica, [tid]) == [tid]
        # The receiving host has explicitly bound its own local source. Neither
        # that path nor session_id is portable; a subsequent assign/pull must
        # preserve both, even if pickup normally evicts this foreign row.
        workspace.set_workspace_path(node.conn, tid, path)
        with kb.write_txn(node.conn):
            node.conn.execute("UPDATE tasks SET session_id='local-session' WHERE id=?", (tid,))
        hub.enter()
        with kb.write_txn(hub.conn):
            hub.conn.execute("UPDATE tasks SET assignee='new-profile' WHERE id=?", (tid,))
        hub.round()
        node.round()
        node.enter()
        worker.evict(node.conn)
        assert worker.import_cards(node.conn, replica, [tid]) == [tid]
        actual = kb.get_task(node.conn, tid)
        assert actual.workspace_path == str(path)
        assert actual.session_id == "local-session"
        assert not [event for event in kb.list_events(node.conn, tid) if event.kind == "imported"]
    finally:
        replica.close()


@pytest.mark.parametrize("kind,has_created_path", [("dir", True), ("dir", False), ("worktree", True)])
def test_missing_binding_parks_once_instead_of_retrying(
    kanban_home, tmp_path, all_assignees_spawnable, kind, has_created_path,
):
    path = tmp_path / "still-existing-source"
    path.mkdir()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="lost local binding", assignee="a", workspace_kind=kind,
                             workspace_path=str(path) if has_created_path else None)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_path=NULL WHERE id=?", (tid,))
        spawned = []
        dispatch.dispatch_once(conn, spawn_fn=lambda *a, **kw: spawned.append(a) or 1234, failure_limit=3)
        task = kb.get_task(conn, tid)
        assert spawned == []
        assert task.status == "triage"
        assert task.consecutive_failures == 1
        assert "missing local workspace binding" in task.last_failure_error
        if has_created_path:
            assert str(path) in task.last_failure_error
            assert "exists" in task.last_failure_error
        events = [e for e in kb.list_events(conn, tid) if e.kind == "gave_up"]
        assert events[-1].payload["sticky"] is True
        assert events[-1].payload["workspace_config_error"] is True
        for _ in range(3):
            kb.recompute_ready(conn)
            dispatch.dispatch_once(conn, spawn_fn=lambda *a, **kw: spawned.append(a) or 1234)
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert spawned == []
        # Diagnostics never copy a synced event's path into this host's binding.
        assert kb.get_task(conn, tid).workspace_path is None
        assert next(e for e in kb.list_events(conn, tid) if e.kind == "created").payload is not None


def test_unbound_worktree_without_creation_binding_keeps_default_resolver(kanban_home, monkeypatch):
    from hermes_cli import kanban_db_workspace as workspace
    from hermes_cli import kanban_source_dispatch as source

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="default anchor", workspace_kind="worktree")
        task = kb.get_task(conn, tid)
        sentinel = (kanban_home, "branch")
        monkeypatch.setattr(workspace, "resolve_worktree_for_dispatch", lambda *a, **kw: sentinel)
        assert source.resolve(conn, task, board=None, kbw=workspace) == (*sentinel, None)


@pytest.mark.parametrize("kind,path_mode,kept", [
    ("dir", "absent", True), ("worktree", "absent", True),
    ("scratch", "exists", False), ("dir", "null", False), ("worktree", "null", False),
])
def test_eviction_preserves_only_bound_source_rows(gws, tmp_path, kind, path_mode, kept):
    hub, node = gws
    hub.enter()
    tid = kb.create_task(hub.conn, title="foreign copy", runs_on=[f"machine:{pickup.WORKER}"])
    hub.round()
    node.round()
    node.enter()
    replica = worker.open_replica(node.conn)
    try:
        worker.import_cards(node.conn, replica, [tid])
    finally:
        replica.close()
    path = tmp_path / "source"
    if path_mode == "exists":
        path.mkdir()
    with kb.write_txn(node.conn):
        node.conn.execute("UPDATE kanban_sync_state SET applying=1")
        node.conn.execute("UPDATE tasks SET workspace_kind=?,workspace_path=? WHERE id=?",
                          (kind, None if path_mode == "null" else str(path), tid))
        node.conn.execute("UPDATE kanban_sync_state SET applying=0")
    assert (tid not in worker.evict(node.conn)) is kept


def test_primary_release_carries_missing_binding_disposition(gws, tmp_path):
    import json

    hub, _ = gws
    hub.enter()
    path = tmp_path / "source"
    path.mkdir()
    tid = kb.create_task(hub.conn, title="primary source", assignee="default", workspace_kind="dir",
                         workspace_path=str(path))
    hub.round()
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET workspace_path=NULL WHERE id=?", (tid,))
    hub.tick()
    fields = hub.conn.execute("SELECT fields FROM kanban_sync_outbox WHERE row_uid=? AND op='release'",
                              (tid,)).fetchone()
    command = json.loads(fields[0])
    assert command["force_trip"] is True
    assert "missing local workspace binding" in command["error"]
    # The existing Board force_trip contract owns finalization. This fixture
    # deliberately does not model spawn_failed releases; prove the real queued
    # command and no competing local finalization, not a fabricated Board ACK.
    assert command["outcome"] == "spawn_failed"
    assert command["extra"]["workspace_config_error"] is True
    assert kb.get_task(hub.conn, tid).status == "running"
    assert kb.get_task(hub.conn, tid).consecutive_failures == 0


@pytest.mark.parametrize("payload", [None, "{", "[]", '{"workspace_path": 7}', '{"workspace_path": ""}'])
def test_malformed_created_history_never_invents_worktree_binding(kanban_home, payload):
    from hermes_cli.kanban_missing_workspace import check

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="history", workspace_kind="worktree")
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'", (payload, tid))
        check(conn, kb.get_task(conn, tid))


@pytest.mark.parametrize("kind,path", [("scratch", None), ("dir", "/already-bound"), ("worktree", "/bound")])
def test_existing_bindings_and_scratch_do_not_read_history(kind, path):
    from hermes_cli.kanban_missing_workspace import check

    # None as connection deliberately explodes if the inert fast path reads history.
    check(None, SimpleNamespace(workspace_kind=kind, workspace_path=path))
