"""Published sources share initial registration's external-path reservation."""
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_board as board
from hermes_cli import kanban_source_launch as launch
from hermes_cli import kanban_source_registration as reg
from hermes_cli import kanban_source_resolver as resolver
from hermes_cli import kanban_workspace_source as ws
from tests.hermes_cli import test_kanban_source_registration as helpers
from tests.hermes_cli import test_kanban_source_ownership_budget as owners
from tests.hermes_cli.test_kanban_workspace_source import manifest

setup = helpers.setup


def published(source, tid):
    raw = {**manifest(), "version": 2, "source_kind": "scratch_external",
           "repository": {"id": 1, "full_name": "example/source", "url": "https://github.com/example/source"},
           "branch": f"refs/heads/wt/{tid}", "commit": helpers.git(source, "rev-parse", "HEAD")}
    snapshot = ws.SourceSnapshot(raw, ws.Custody("hub", "published", 1, source_protocol=2))
    resolved = SimpleNamespace(path=str(source), branch=f"wt/{tid}", mode="holder_reuse", manifest_revision=1)
    return snapshot, resolved


@pytest.mark.parametrize("surface", ["holder", "return_home", "attach", "replay"])
def test_published_source_refuses_another_initial_owner(setup, tmp_path, surface):
    conn, b, tid, source, scratch, *_ = setup
    other = owners._second_task(conn, tmp_path, f"wt/{tid}")
    snapshot, resolved = published(source, tid)
    task = kb.get_task(conn, other)
    if surface == "replay":
        b.refuse["source_attach"] = "invalid"
        with pytest.raises(launch.AttachRefused):
            launch.attach(conn, task, str(scratch), resolved, snapshot)
        conn.execute("UPDATE kanban_source_runs SET state='refused' WHERE run_id=?", (task.current_run_id,))
        conn.commit()
        b.refuse.clear()
    first = reg.register_initial(conn, tid, str(source))
    before = helpers.git(source, "rev-parse", "HEAD")
    calls = len(b.calls)
    if surface == "holder":
        with pytest.raises(ws.WorkspaceSourceRefusal, match="occupied_path"):
            resolver.resolve_source_workspace(conn, task, snapshot, me="hub")
    elif surface == "return_home":
        with pytest.raises(ws.WorkspaceSourceRefusal, match="occupied_path"):
            resolver._bring_to(resolver._Bound(60, time.monotonic), conn, other,
                               str(source), snapshot.manifest())
    else:
        with pytest.raises(launch.AttachRefused, match="another active task"):
            launch.attach(conn, task, str(scratch), resolved, snapshot)
    assert helpers.git(source, "rev-parse", "HEAD") == before
    assert len(b.calls) == calls
    assert board.attached_run(conn, first.run_id).state == "attached"
    other_run = board.attached_run(conn, task.current_run_id)
    assert other_run is None or other_run.state == "refused"


def test_published_observation_serializes_with_initial_and_reserves_before_rpc(setup, tmp_path, monkeypatch):
    conn, b, tid, source, scratch, *_ = setup
    other = owners._second_task(conn, tmp_path, f"wt/{tid}")
    snapshot, resolved = published(source, tid)
    original = b.answer
    checked = []
    ownership = reg._owned_elsewhere

    def locked_owner(connection, task_id, path):
        if task_id == tid:
            assert connection.in_transaction, "published ownership must serialize with initial registration"
        return ownership(connection, task_id, path)

    monkeypatch.setattr(reg, "_owned_elsewhere", locked_owner)

    def answer(body):
        if body.get("action") == "source_attach":
            with pytest.raises(launch.AttachRefused, match="another active task"):
                reg.register_initial(conn, other, str(source))
            checked.append(True)
        return original(body)

    b.answer = answer
    assert launch.attach(conn, kb.get_task(conn, tid), str(scratch), resolved, snapshot).state == "attached"
    assert checked


def test_published_attach_rechecks_competitor_after_identity_proof(setup, tmp_path, monkeypatch):
    conn, _b, tid, source, scratch, *_ = setup
    other = owners._second_task(conn, tmp_path, f"wt/{tid}")
    snapshot, resolved = published(source, tid)
    original = reg.source_identity
    raced = []

    def identity(*args, **kwargs):
        if not raced:
            raced.append(True)
            reg.register_initial(conn, tid, str(source))
        return original(*args, **kwargs)

    monkeypatch.setattr(reg, "source_identity", identity)
    with pytest.raises(launch.AttachRefused, match="another active task"):
        launch.attach(conn, kb.get_task(conn, other), str(scratch), resolved, snapshot)
    assert raced
    assert board.attached_run(conn, kb.get_task(conn, other).current_run_id) is None


def test_return_home_does_not_fast_forward_another_tasks_source(setup, tmp_path):
    conn, _, tid, source, _, _, clone = setup
    reg.register_initial(conn, tid, str(source))
    other = owners._second_task(conn, tmp_path, f"wt/{tid}")
    snapshot, _ = published(source, tid)
    before = helpers.git(source, "rev-parse", "HEAD")
    helpers.git(clone, "-c", "user.name=Test", "-c", "user.email=test@example.com",
                "commit", "--allow-empty", "-m", "next published commit")
    snapshot.manifest_value["commit"] = helpers.git(clone, "rev-parse", "HEAD")
    assert snapshot.manifest_value["commit"] != before
    with pytest.raises(ws.WorkspaceSourceRefusal, match="occupied_path"):
        resolver._bring_to(resolver._Bound(60, time.monotonic), conn, other, str(source), snapshot.manifest())
    assert helpers.git(source, "rev-parse", "HEAD") == before
