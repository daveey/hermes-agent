"""Source heartbeat response and optional completion metadata contracts."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_registration as registration
from tests.tools import test_kanban_source_heartbeat as heartbeat

_bind, setup = heartbeat._bind, heartbeat.setup  # shared pytest fixture
from tools import kanban_tools as kt
from tools import kanban_tools_source as surface


def test_heartbeat_returns_registered_external_path(setup, monkeypatch):
    conn, _, tid, path, _, _, _ = setup
    run_id = kb.get_task(conn, tid).current_run_id
    _bind(monkeypatch, conn, tid, run_id, "owner")
    result = json.loads(kt._handle_heartbeat({"task_id": tid, "source_path": str(path)}))
    assert result["source"]["path"] == str(path)


@pytest.mark.parametrize("metadata", [None, {}, {"note": "not an artifact selection"}])
def test_completion_without_artifact_selection_preserves_metadata(setup, metadata):
    conn, _, tid, path, _, _, _ = setup
    run = registration.register_initial(conn, tid, str(path))
    assert surface.stage_completion(conn, tid, metadata, expected_run_id=run.run_id) == metadata
