"""CLI placement must judge the assignee persisted on the card."""
import json

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_placement as placement
from tests.hermes_cli import test_kanban_portable_reason_audit as audit

kanban_home = audit.kanban_home


@pytest.mark.parametrize("pooled", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("agent", [False, True])
def test_cli_placement_uses_resolved_assignee(kanban_home, monkeypatch, pooled, explicit, agent):
    profile = kanban_home / "profiles" / "specialist"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("toolsets: [terminal]\n")
    cfg = {"assignees": ["default"]}
    if pooled:
        cfg["worker_pools"] = {"hub": ["worker-1"]}
    monkeypatch.setattr(placement, "config", lambda: cfg)
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(cli, "_agent_invoked", lambda: agent)
    if agent:
        from tests.hermes_cli.test_kanban_selfwork_entry import register_chat
        monkeypatch.setenv('HERMES_SESSION_ID', register_chat(kanban_home, '20261123_120000_assignee'))
    monkeypatch.setattr(cli, "_profile_author", lambda: "specialist" if agent else "default")
    monkeypatch.setattr(kb, "_default_ready_assignee", lambda: "specialist")
    extra = " --assignee default" if explicit else ""
    result = json.loads(cli.run_slash("create 'Analyze public data' --requires terminal --json" + extra))
    with kbc.connect() as conn:
        task = kb.get_task(conn, result["id"])
        payload = next(e.payload for e in kb.list_events(conn, task.id) if e.kind == "created")
    assert task.assignee == ("default" if explicit else "specialist")
    assert payload["portable_reason"] == (None if explicit else "assignee not allowlisted")
    expected_policy = "worker-first-v1" if pooled and explicit else None
    assert task.placement_policy == expected_policy
    assert payload["placement_policy"] == expected_policy
