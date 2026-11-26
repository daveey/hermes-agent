"""Admission receipts survive tool normalization and transactional creation."""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard
from dataclasses import asdict
import sqlite3

import pytest

from hermes_cli import kanban_db_creation as creation
from hermes_cli.kanban_hub_admission import Verdict
from tools import kanban_tools_placement as tools
from tests.hermes_cli import test_kanban_placement as placement_support

mac = placement_support.mac


@pytest.fixture
def receipt():
    return asdict(Verdict(admit=False, why="portable log read"))


def test_creation_audit_carries_preflight_receipt(monkeypatch, receipt):
    from hermes_cli import kanban_worker_placement as worker
    monkeypatch.setattr(worker, "creation_audit", lambda fields: None)
    result = creation.tool_creation_audit({"title": "logs"},
                                          admission={"hub_admission": receipt})
    assert result == {"hub_admission": receipt}


def test_created_emits_separate_admission_event(monkeypatch, receipt):
    from hermes_cli import kanban_db as kb
    events = []
    monkeypatch.setattr(kb, "_append_event", lambda conn, tid, kind, payload:
                        events.append((kind, payload)))
    from hermes_cli import kanban_consent
    monkeypatch.setattr(kanban_consent, "snapshot", lambda *_a: [])  # minimal connection has no consent rows
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, priority INTEGER, created_by TEXT, session_id TEXT)")
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?)", ("t_test", 137, "stored-author", "stored-session"))
        creation.record_created(conn, "t_test", assignee="default", status="ready", parents=[],
                                creator_task_id=None, tenant=None, workspace_kind="scratch",
                                workspace_path=None, branch_name=None, project_id=None, skills=[],
                                goal_mode=False, model_override=None, provider_override=None,
                                created_by="test", requires=["hub:write"],
                                placement_policy="worker-first-v1",
                                placement_audit={"hub_admission": receipt})
    assert [payload["priority"] for kind, payload in events if kind == "created"] == [137]
    assert [payload["creator_source"] for kind, payload in events if kind == "created"] == [
        {"author": "stored-author", "session_id": "stored-session"},
    ]
    assert [payload for kind, payload in events if kind == "hub_admission"] == [receipt]


def test_admitted_local_response_keeps_receipt(receipt):
    receipt = {**receipt, "admit": True}
    marker = {"where": "local", "why": "hub context", "hub_admission": receipt}
    result = tools._marker_placement_fields(None, "t_test", marker, None)
    assert result["placement"]["hub_admission"] == receipt


def test_refusal_returns_structured_receipt(receipt):
    import json
    result = json.loads(tools._placement_refused("hub", "portable", hub_admission=receipt))
    assert result["hub_admission"] == receipt
    assert result["created"] is False


@pytest.mark.parametrize("admit", [True, False])
def test_origin_bound_creation_does_not_skip_gate(monkeypatch, admit):
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_hub_admission as judge
    calls = []
    monkeypatch.setattr(kp, "config", lambda: {"worker_pools": {"hub": ["worker-1"]}})
    monkeypatch.setattr(kp.reg, "self_gateway", lambda: "hub")
    def fake(*args, **kwargs):
        calls.append(args)
        return Verdict(admit=admit, need="hub_chrome" if admit else None,
                       evidence="Chrome" if admit else "", why="test")
    monkeypatch.setattr(judge, "admit", fake)
    result = tools._origin_enrollment({"title": "Chrome", "placement": "local"}, object(), None)
    assert result["hub_admission"]["admit"] is admit
    assert len(calls) == 1
    # placement='local' on the hub is an explicit stay-here pin: a deny refuses (spec owner 11-12)
    assert result.get("where") == ("local" if admit else "refuse")
    assert result.get("placement_policy") is None


@pytest.mark.parametrize("admit", [True, False])
def test_tool_create_persists_verdict_and_contract(mac, monkeypatch, admit):
    import json
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb, kanban_placement as kp
    from hermes_cli import kanban_hub_admission as judge
    from hermes_cli import kanban_capability_routing as routing
    monkeypatch.setattr(kp, "config", lambda: {"worker_pools": {"hub": ["worker-1"]}})
    monkeypatch.setattr(kp.reg, "self_gateway", lambda: "hub")
    monkeypatch.setattr(routing, "preflight", lambda *a, **kw: None)
    monkeypatch.setattr(judge, "admit", lambda *a, **kw: Verdict(admit=admit, why="test verdict"))
    out = json.loads(kt._handle_create({"title": "Read logs on the hub", "requires": []}))
    assert out.get("ok"), out
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        task = kb.get_task(conn, out["task_id"])
        events = kb.list_events(conn, task.id)
    receipts = [event.payload for event in events if event.kind == "hub_admission"]
    assert len(receipts) == 1
    assert receipts[0]["admit"] is admit
    assert task.placement_policy == (None if admit else "worker-first-v1")
    if not admit:
        assert "hub:write" in task.requires
