"""Published v2 custody is bound to repository identity, including unchanged endings."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_board as board_calls
from hermes_cli import kanban_source_launch as launch
from hermes_cli import kanban_source_publish as pub
from hermes_cli import kanban_source_registration as reg
from hermes_cli import kanban_workspace_source as ws
from tests.hermes_cli import test_kanban_source_publish as helpers
from tests.hermes_cli import test_kanban_source_registration as fixtures
from tests.hermes_cli.test_kanban_workspace_source import manifest

world = helpers.world
board = helpers.board
card = helpers.card
setup = fixtures.setup


def _attach(setup, fault=None):
    conn, _b, tid, source, scratch, _home, _clone = setup
    raw = {**manifest(), "version": 2, "source_kind": "scratch_external",
           "repository": {"id": 1, "full_name": "example/source", "url": "https://github.com/example/source"}}
    if fault == "id":
        raw["repository"]["id"] = 2
    elif fault == "name":
        raw["repository"]["full_name"] = "example/other"
        raw["repository"]["url"] = "https://github.com/example/other"
    snapshot = ws.SourceSnapshot(raw, ws.Custody("other", "published", 1, source_protocol=2))
    resolved = SimpleNamespace(path=str(source), branch=f"wt/{tid}", mode="materialized", manifest_revision=1)
    return launch.attach(conn, kb.get_task(conn, tid), str(scratch), resolved, snapshot)


def test_published_attach_captures_identity_before_rpc(setup):
    conn, b, tid, _source, _scratch, _home, clone = setup
    answer = b.answer
    expected = {"repository_id": 1, "full_name": "example/source", "common_dir": str(clone / ".git"),
                "source_kind": "scratch_external"}

    def inspect(body):
        assert board_calls.attached_run(conn, kb.get_task(conn, tid).current_run_id).source_identity == expected
        return answer(body)

    b.answer = inspect
    assert _attach(setup).source_identity == expected


@pytest.mark.parametrize("fault", ["id", "name"])
def test_published_attach_refuses_manifest_repository_mismatch(setup, fault):
    conn, b, tid, *_ = setup
    before = len(b.calls)
    with pytest.raises(launch.AttachRefused, match="source_identity_refused"):
        _attach(setup, fault)
    assert len(b.calls) == before
    assert board_calls.attached_run(conn, kb.get_task(conn, tid).current_run_id) is None


def test_protocol2_attach_without_manifest_is_typed_refusal(setup):
    conn, b, tid, source, scratch, _home, _clone = setup
    snapshot = ws.SourceSnapshot(None, ws.Custody("other", "published", 1, source_protocol=2))
    resolved = SimpleNamespace(path=str(source), branch=f"wt/{tid}", mode="materialized", manifest_revision=1)
    before = len(b.calls)
    with pytest.raises(launch.AttachRefused, match="source_identity_refused"):
        launch.attach(conn, kb.get_task(conn, tid), str(scratch), resolved, snapshot)
    assert len(b.calls) == before
    assert board_calls.attached_run(conn, kb.get_task(conn, tid).current_run_id) is None


def _v2(card):
    run = replace(card.run(), source_protocol=2,
                  source_identity=reg.source_identity(str(card.path), "task_worktree"))
    board_calls.record_run(card.conn, run)
    assert card.receipt()["state"] == "published"
    return card.run()


@pytest.mark.parametrize("fault", ["missing", "repository_id", "full_name", "common_dir", "source_kind"])
@pytest.mark.parametrize("surface", ["preflight", "publisher"])
def test_v2_unchanged_refuses_changed_or_missing_identity(card, fault, surface):
    run = _v2(card)
    changed = None if fault == "missing" else {**run.source_identity, fault: "wrong"}
    board_calls.record_run(card.conn, replace(run, source_identity=changed))
    card.board.calls.clear()
    if surface == "preflight":
        receipt = card.receipt()
        assert receipt["state"] == "retained"
        assert receipt["reason"] == "publish_error=source_identity_refused"
    else:
        with pytest.raises(pub.PublishRefused, match="source_identity_refused"):
            pub.publish(card.conn, kb.get_task(card.conn, card.id), card.run(), card.cfg,
                        pub._Deadline(90), machine="hub")
    assert "publish_source" not in card.board.actions()


def test_v2_new_commit_identity_refusal_precedes_push(card):
    run = _v2(card)
    old = card.remote()
    helpers.commit(card.path, "next.txt", "new")
    board_calls.record_run(card.conn, replace(run, source_identity=None))
    assert card.receipt()["reason"] == "publish_error=source_identity_refused"
    assert card.remote() == old


def test_artifact_upgrade_persists_identity_for_next_ending(card, monkeypatch):
    from hermes_cli import kanban_source_artifacts as artifacts
    monkeypatch.setattr(artifacts, "portable", lambda *a, **k: [
        {"attachment_uid": "selected", "filename": "report.txt", "sha256": "a" * 64}])
    assert card.receipt()["state"] == "published"
    assert card.run().source_protocol == 2
    assert card.run().source_identity == reg.source_identity(str(card.path), "task_worktree")
    assert card.receipt()["state"] == "unchanged"
