"""Reject unsatisfiable label conjunctions before they strand Board cards. [daveey]"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.test_kanban_worker_node import HUB, MACHINES

pytest_plugins = ["tests.hermes_cli.test_kanban_requires_placement"]


@pytest.mark.parametrize("labels", [["hermes-hub"], ["linux", "macos"]])
@pytest.mark.parametrize("requires", [[], ["terminal"]])
def test_create_refuses_labels_no_enrolled_machine_carries(gws, labels, requires):
    hub, _ = gws
    before = hub.conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    with pytest.raises(ValueError, match="no enrolled machine"):
        kb.create_task(hub.conn, title="unclaimable", assignee="default", requires=requires, runs_on=labels)
    assert hub.conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == before


def test_tool_refusal_is_actionable_and_writes_nothing(gws, monkeypatch):
    from tools import kanban_tools as kt
    hub, _ = gws
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    before = hub.conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    out = json.loads(kt._handle_create({"title": "unclaimable", "assignee": "default", "requires": [],
                                      "runs_on": ["hermes-hub"], "placement": "local"}))
    assert "no enrolled machine" in out.get("error", ""), out
    assert "placement" in out["error"]
    assert hub.conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == before


@pytest.mark.parametrize("labels", [[], ["linux"], [f"machine:{HUB}"]])
def test_registered_labels_are_not_rejected_while_machines_held(gws, monkeypatch, labels):
    hub, _ = gws
    pool = {name: {**entry, "hold": True} for name, entry in MACHINES.items()}
    monkeypatch.setattr(wn, "registry", lambda: {"machines": pool})
    assert kb.create_task(hub.conn, title="held not impossible", assignee="default", requires=[], runs_on=labels)


def test_unknown_registry_does_not_claim_labels_are_impossible(gws, monkeypatch):
    hub, _ = gws
    monkeypatch.setattr(wn, "registry", lambda: None)
    assert kb.create_task(hub.conn, title="unknown", assignee="default", requires=[], runs_on=["custom"])
