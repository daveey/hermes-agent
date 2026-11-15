"""Artifact-only revisions must pass both existing unchanged fast paths."""
from dataclasses import replace

from hermes_cli import kanban_source_board as board_calls
from hermes_cli import kanban_source_publish as pub
from hermes_cli import kanban_source_registration as registration
from hermes_cli import kanban_source_artifacts as artifacts
from hermes_cli import kanban_workspace_source as ws
from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_source_publish as helpers

world = helpers.world
board = helpers.board
card = helpers.card


def test_artifact_only_change_cannot_use_either_unchanged_shortcut(card, monkeypatch):
    selection = []
    monkeypatch.setattr(artifacts, "portable", lambda *a, **k: list(selection), raising=False)
    assert card.receipt()["state"] == "published"
    selection.append({"attachment_uid": "artifact-one", "filename": "report.txt", "sha256": "a" * 64})
    # Full preflight's shortcut.
    receipt = card.receipt()
    assert receipt["state"] == "published"
    assert receipt["manifest_revision"] == 2
    assert card.run().manifest["artifacts"] == selection
    assert card.run().manifest["version"] == 2
    selection.append({"attachment_uid": "artifact-two", "filename": "report2.txt", "sha256": "b" * 64})
    # Direct publisher's independent shortcut.
    published = pub.publish(card.conn, kb.get_task(card.conn, card.id), card.run(), card.cfg,
                            pub._Deadline(90), machine="hub")
    assert published.revision == 3
    assert published.manifest["artifacts"] == selection
    assert not published.pushed
    ws.parse_manifest(published.manifest)


def test_scratch_first_publish_preserves_kind_and_protocol(card, monkeypatch):
    monkeypatch.setattr(artifacts, "portable", lambda *a, **k: [], raising=False)
    run = replace(card.run(), mode="initial", source_protocol=2,
                  source_identity=registration.source_identity(str(card.path), "scratch_external"))
    board_calls.record_run(card.conn, run)
    assert card.receipt()["state"] == "published"
    assert card.run().manifest["source_kind"] == "scratch_external"
    assert card.run().manifest["version"] == 2


def test_unavailable_selected_bytes_retain_even_when_git_unchanged(card, monkeypatch):
    assert card.receipt()["state"] == "published"

    def unavailable(*a, **kw):
        raise artifacts.ArtifactRefused("artifact_unavailable", "offline")

    monkeypatch.setattr(artifacts, "portable", unavailable, raising=False)
    receipt = card.receipt()
    assert receipt["state"] == "retained"
    assert "artifact_unavailable" in receipt["reason"]
