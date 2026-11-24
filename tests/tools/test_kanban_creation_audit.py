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
    ("local-pin", None, "worker-first-v1"),
])
def test_effective_creation_audit_preserves_enrollment(kanban_home, tmp_path, monkeypatch, case, reason, policy):
    from hermes_cli import kanban_hub_admission as judge
    calls = []
    def deny(*args, **kwargs):
        calls.append(args)
        return judge.Verdict(admit=False, why="Public analysis needs no hub context")
    monkeypatch.setattr(judge, "admit", deny)
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    monkeypatch.setattr(kt, "_persisted_identity", lambda: "specialist" if case == "implicit-assignee" else "default")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_constants import get_hermes_home
    from tests.tools.test_kanban_create_overlap import register_chat
    chat = register_chat(get_hermes_home(), "20261123_120000_audit_owner")
    monkeypatch.setenv("HERMES_SESSION_ID", chat)
    args = {"title": "Analyze public data", "requires": []}
    project_id = None
    if case in {"inherited-project", "empty-project"}:
        repo = tmp_path / "repo"
        repo.mkdir()
        with projects_db.connect_closing() as conn:
            project_id = projects_db.create_project(conn, name="Audit", primary_path=str(repo))
        with kbc.connect() as conn:
            parent = kb.create_task(conn, title="Project owner", assignee="default", project_id=project_id, session_id=chat)
        monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
        if case == "empty-project":
            args["project"] = ""
    if case == "local-pin":
        args["placement"] = "local"
    result = json.loads(kt._handle_create(args))
    if case in {"inherited-project", "implicit-assignee", "local-pin"}:
        # Spec owner 11-12: a structural keep-local or an explicit stay-here pin on the hub is
        # judged, and (unable to go to the pool) a deny refuses the create with the verdict.
        assert not result.get("ok") and result.get("hub_admission", {}).get("admit") is False, result
        assert len(calls) == 1
        return
    assert result.get("ok"), result
    assert calls == []
    with kbc.connect() as conn:
        task = kb.get_task(conn, result["task_id"])
        event = next(e for e in kb.list_events(conn, task.id) if e.kind == "created")
        assert task.placement_policy == policy
        assert task.assignee == ("specialist" if case == "implicit-assignee" else "default")
        assert task.project_id == (project_id if case == "inherited-project" else None)
        assert task.workspace_kind == ("worktree" if case == "inherited-project" else "scratch")
        assert event.payload["portable_reason"] == reason
        assert "placement_policy" in event.payload
        assert event.payload["placement_policy"] == policy
