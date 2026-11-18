"""Snapshot staging must not launder denied originals into trusted cache paths."""
from pathlib import Path

import pytest

from gateway.platforms.base import validate_media_delivery_path
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_artifacts as artifacts
from hermes_cli import kanban_source_registration as registration
from tests.hermes_cli import test_kanban_source_registration as helpers
from tools import kanban_tools_source as surface

setup = helpers.setup


@pytest.mark.parametrize("entrypoint", ["heartbeat", "completion", "stage"])
@pytest.mark.parametrize("denied", [True, False])
def test_original_delivery_policy_before_snapshot(setup, tmp_path, monkeypatch, entrypoint, denied):
    conn, _, tid, source, _, _, _ = setup
    run = registration.register_initial(conn, tid, str(source))
    home = tmp_path / "user-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HERMES_MEDIA_ALLOW_DIRS", raising=False)
    monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "0")
    original = home / (".ssh" if denied else "reports") / "config"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"harmless fixture report")
    assert bool(validate_media_delivery_path(str(original))) is not denied
    published = []
    publish = artifacts.blobs._publish_file

    def capture(path, data):
        published.append(path)
        return publish(path, data)

    monkeypatch.setattr(artifacts.blobs, "_publish_file", capture)

    def select():
        args = {"artifacts": [str(original)]}
        if entrypoint == "heartbeat":
            return surface.heartbeat_source(conn, tid, args, worker_task=tid, expected_run_id=run.run_id)
        if entrypoint == "completion":
            return surface.stage_completion(conn, tid, args, expected_run_id=run.run_id)
        return artifacts.stage(conn, tid, args["artifacts"], board="default", run_uid=run.run_uid)

    if denied:
        error = kb.ArtifactPreservationError if entrypoint == "completion" else ValueError
        with pytest.raises(error, match="delivery policy"):
            select()
        assert published == []
        assert kb.list_attachments(conn, tid) == []
        assert artifacts.selected(conn, tid, run.run_uid) is None
    else:
        select()
        entries = artifacts.selected(conn, tid, run.run_uid)
        assert len(entries) == 1
        assert len(published) == 1
        row = artifacts._row(conn, tid, entries[0])
        assert Path(row["stored_path"]).read_bytes() == original.read_bytes()
