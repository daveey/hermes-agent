"""Artifact retry memo must also apply to the current source holder."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_workspace_source as ws
from tests.hermes_cli.test_kanban_workspace_source import enabled, manifest


@pytest.mark.parametrize("holder", ["here", "elsewhere"])
@pytest.mark.parametrize("code,expiry", [("artifact_unavailable", 60), ("artifact_mismatch", 21600)])
def test_artifact_retry_same_revision_including_holder(tmp_path, monkeypatch, holder, code, expiry):
    conn = sqlite3.connect(tmp_path / "board.db")
    now = [1000.0]
    monkeypatch.setattr(ws.time, "time", lambda: now[0])
    raw = manifest()
    card = {"id": "task", "workspace_source": json.dumps(raw), "workspace_custody": json.dumps({
        "source_holder": holder, "state": "published", "manifest_revision": raw["manifest_revision"]})}
    cfg = enabled()
    try:
        assert ws.WorkspaceSourceRefusal(code).scope == "host"
        ws.remember_skip(conn, "task", raw["manifest_revision"], code)
        conn.commit()
        for elapsed in (0, expiry - 1):
            now[0] = 1000 + elapsed
            assert code in ws.pickup_skip(conn, card, "here", cfg=cfg, proven_ids={raw["repository"]["id"]})
        now[0] = 1000 + expiry
        assert ws.pickup_skip(conn, card, "here", cfg=cfg, proven_ids={raw["repository"]["id"]}) is None
    finally:
        conn.close()
