"""Creation diagnostics must describe the effective tool fields, not omitted arguments."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_placement as placement
from hermes_cli import projects_db
from tests.hermes_cli import test_kanban_portable_reason_audit as audit_support
from tools import kanban_tools as kt

kanban_home = audit_support.kanban_home


@pytest.mark.parametrize("case,reason,policy", [
    ("inherited-project", "uncarried option", None),
    ("implicit-assignee", "assignee not allowlisted", None),
    ("portable", None, "worker-first-v1"),
    ("empty-project", None, "worker-first-v1"),
    ("local-pin", None, None),
])
def test_effective_creation_audit_preserves_enrollment(kanban_home, tmp_path, monkeypatch, case, reason, policy):
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    monkeypatch.setattr(kt, "_persisted_identity", lambda: "specialist" if case == "implicit-assignee" else "default")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    args = {"title": "Analyze public data", "requires": []}
    project_id = None
    if case in {"inherited-project", "empty-project"}:
        repo = tmp_path / "repo"
        repo.mkdir()
        with projects_db.connect_closing() as conn:
            project_id = projects_db.create_project(conn, name="Audit", primary_path=str(repo))
        with kbc.connect() as conn:
            parent = kb.create_task(conn, title="Project owner", assignee="default", project_id=project_id)
        monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
        if case == "empty-project":
            args["project"] = ""
    if case == "local-pin":
        args["placement"] = "local"
    result = json.loads(kt._handle_create(args))
    assert result.get("ok"), result
    with kbc.connect() as conn:
        task = kb.get_task(conn, result["task_id"])
        event = next(e for e in kb.list_events(conn, task.id) if e.kind == "created")
        assert task.placement_policy == policy
        assert task.assignee == ("specialist" if case == "implicit-assignee" else "default")
        assert task.project_id == (project_id if case == "inherited-project" else None)
        assert task.workspace_kind == ("worktree" if case == "inherited-project" else "scratch")
        assert event.payload["portable_reason"] == reason
