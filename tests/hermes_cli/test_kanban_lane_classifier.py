"""Unmatched chat cards get a bounded semantic fallback, never under a write lock."""
import json
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_lanes as lanes
from tests.hermes_cli import test_kanban_lanes as helpers

conn = helpers.conn
person = helpers.person
configured = helpers.configured


@pytest.fixture
def semantic(configured):
    configured({
        "hermes": {"share": 20, "gated": True, "describe": "Agent core, Desktop, workers, fairness and Kanban"},
        "research": {"share": 10, "describe": "H3, Terraform, glimpse-lm research runs and papers"},
        "other": {"share": 10, "describe": "Anything else"},
    })


@pytest.mark.parametrize("title,expected", [
    ("Find and fix the producer of Desktop turn-lease loss", "hermes"),
    ("Worker drain Stage 2", "hermes"),
    ("Fairness Phase A", "hermes"),
    ("H3 recovery", "research"),
])
def test_unmatched_title_classified_outside_transaction(conn, semantic, monkeypatch, title, expected):
    calls = []

    def classify(pol, opts):
        assert not conn.in_transaction
        calls.append((pol, opts))
        return expected

    monkeypatch.setattr(lanes, "_fallback_lane", classify, raising=False)
    tid = kb.create_task(conn, title=title, body="context", assignee="default", created_by="default")
    row = kb.get_task(conn, tid)
    assert row.tenant == expected
    assert row.status == "ready"  # chat work must not acquire the self-work gate
    event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='lane_assigned'", (tid,)).fetchone()
    assert json.loads(event[0])["source"] == "classified"
    assert len(calls) == 1


@pytest.mark.parametrize("error", [OSError("offline"), subprocess.TimeoutExpired("ask", 5)])
def test_error_falls_back(conn, semantic, monkeypatch, error):
    def fail(*args):
        raise error

    monkeypatch.setattr(lanes, "_fallback_lane", fail, raising=False)
    assert lanes._classify(conn, lanes.policy(), None, None, {"title": "H3 recovery"}) == ("other", "unclassified")


@pytest.mark.parametrize("answer", ["unknown", "", None])
def test_unknown_answer_falls_back(conn, semantic, monkeypatch, answer):
    monkeypatch.setattr(lanes, "_fallback_lane", lambda *a: answer, raising=False)
    assert lanes._classify(conn, lanes.policy(), None, None, {}) == ("other", "unclassified")


@pytest.mark.parametrize("options,expected", [
    ({"workspace_path": "/src/hermes-agent"}, "hermes"),
    ({"tenant": "babybear"}, "babybear"),
])
def test_rule_and_given_make_no_call(conn, configured, monkeypatch, options, expected):
    def forbidden(*args):
        pytest.fail("unnecessary LLM call")

    monkeypatch.setattr(lanes, "_fallback_lane", forbidden, raising=False)
    tid = kb.create_task(conn, title="work", assignee="default", created_by="user", **options)
    assert kb.get_task(conn, tid).tenant == expected


def test_shadow_makes_no_call(conn, configured, monkeypatch):
    configured(lane_shadow=True)
    monkeypatch.setattr(lanes, "_fallback_lane", lambda *a: pytest.fail("shadow LLM call"), raising=False)
    tid = kb.create_task(conn, title="H3 recovery", assignee="default")
    assert kb.get_task(conn, tid).tenant == "other"


def test_inheritance_and_gating_unchanged(conn, configured, monkeypatch):
    parent = kb.create_task(conn, title="parent", assignee="default", tenant="hermes", created_by="user")
    monkeypatch.setattr(lanes, "_fallback_lane", lambda *a: pytest.fail("inherited LLM call"), raising=False)
    tid = kb.create_task(conn, title="child", assignee="default", creator_task_id=parent, created_by="default")
    assert kb.get_task(conn, tid).tenant == "hermes"
    assert kb.get_task(conn, tid).status == "triage"


def test_outer_transaction_never_calls_llm(conn, semantic, monkeypatch):
    monkeypatch.setattr(lanes, "_fallback_lane", lambda *a: pytest.fail("LLM under outer transaction"), raising=False)
    with kb.write_txn(conn):
        tid = kb.create_task(conn, title="H3 recovery", assignee="default")
    assert kb.get_task(conn, tid).tenant == "other"
