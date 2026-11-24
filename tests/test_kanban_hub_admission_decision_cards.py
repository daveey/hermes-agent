"""Blocked decision cards on the hub skip the hub admission judge (t_01a1cf9334ff49bc). [daveey]

A card created ``initial_status='blocked'`` with a ``human_request`` holds no slot while it waits for David, so it
is created wherever the caller is with no judge call. Its work, once unblocked, goes to the worker pool (the
same route a denied card takes), so the skip never puts unjudged work on a hub slot. A hub-origin refusal names
an alternative that works there, never ``placement='local'`` (which is what the caller passed).
"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_hub_admission as judge
from hermes_cli import kanban_placement as placement
from tests.hermes_cli import hub_judge_support
from tests.hermes_cli import test_kanban_portable_reason_audit as audit_support
from tools import kanban_tools as kt

kanban_home = audit_support.kanban_home
hub_judge = hub_judge_support.hub_judge

POOLS = {"worker_pools": {"hub": ["worker-1"]}}
QUEUE_CFG = {**POOLS, "worker_queue": {"hub": "pool:worker"}}
ASK = {"action": "Reply A or B in this chat. Unblock = build the pick; archive = drop it.",
       "why_human": "a product choice only David can make", "interaction": "reply",
       "scope": "this decision only", "evidence": "two designs were drafted"}
HUB_TEXT = "Pick a retention window for ~/.hermes/logs on the hub"


@pytest.fixture
def gate(monkeypatch):
    calls = []
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKER_TASK", raising=False)

    def fake(fields, *, hub_reason=None):
        calls.append((fields, hub_reason))
        return judge.Verdict(admit=False, why="no hub-only capability is required")
    monkeypatch.setattr(judge, "admit", fake)
    return calls


def decide(fields, cfg=POOLS, machine_ok=None):
    return placement.decide(fields, self_gw="hub", cfg=cfg, live=lambda _: None, carries=True,
                            machine_ok=machine_ok)


def decision_card(**extra):
    return {"title": HUB_TEXT, "initial_status": "blocked", "human_request": dict(ASK), **extra}


@pytest.mark.parametrize("pin", [None, "auto", "local", "here", " Local "])
def test_blocked_decision_card_on_hub_makes_no_judge_call(gate, pin):
    fields = decision_card(**({"placement": pin} if pin is not None else {}))
    result = decide(fields)
    assert gate == []
    assert result["where"] == "local"
    # After unblock the work goes to the pool, never to a hub slot.
    assert result.get("placement_policy") == "worker-first-v1"
    assert "hub:write" in result["requires"]
    assert result["hub_admission"]["skipped"] == "blocked_decision_card"


def test_blocked_decision_card_follows_worker_queue(gate):
    result = decide(decision_card(), cfg=QUEUE_CFG, machine_ok=lambda labels: "pool:worker" in labels)
    assert gate == []
    assert result["runs_on"][0] == "pool:worker"
    assert "capability:hub-write" in result["runs_on"]
    assert "placement_policy" not in result


def test_blocked_decision_card_without_pool_is_created_here(gate):
    result = placement.decide(decision_card(), self_gw="hub", cfg={})
    assert gate == []
    assert result["where"] == "local"


@pytest.mark.parametrize("fields", [
    {"title": HUB_TEXT},  # ready card with hub wording
    {"title": HUB_TEXT, "initial_status": "blocked"},  # blocked with no human_request
    decision_card(placement="hub"),  # explicit hub pin: the caller asked for a hub slot
    decision_card(runs_on=["machine:hub"]),
    decision_card(workspace_kind="dir", workspace_path="/srv/x"),  # host workspace: it would run here later
])
def test_cards_that_would_take_a_hub_slot_are_still_judged(gate, fields):
    decide(fields)
    assert len(gate) == 1


def test_nonhub_decision_card_unchanged(gate):
    result = placement.decide(decision_card(), self_gw="mac", cfg=POOLS, live=lambda _: None, carries=True)
    assert gate == []
    assert "hub_admission" not in result


@pytest.mark.parametrize("pin", ["local", "here"])
def test_hub_local_pin_refusal_names_a_working_alternative(gate, pin):
    result = decide({"title": HUB_TEXT, "placement": pin})
    assert result["where"] == "refuse"
    from tools.kanban_tools_placement import _placement_refused
    error = json.loads(_placement_refused(result.get("pin", "hub"), result.get("why", ""),
                                          hub_admission=result.get("hub_admission"),
                                          remedy=result.get("remedy")))["error"]
    assert "Use placement='local'" not in error
    assert "omit placement" in error and "hub_reason" in error and "initial_status='blocked'" in error


# --- the kanban_create tool on the hub gateway --------------------------------------------------


@pytest.fixture
def on_hub(kanban_home, monkeypatch, hub_judge):
    from tests.tools.test_kanban_create_overlap import register_chat
    monkeypatch.setenv("HERMES_SESSION_ID", register_chat(kanban_home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKER_TASK", raising=False)
    monkeypatch.setattr(kt, "_persisted_identity", lambda: "default")
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    hub_judge.admit_next = False
    return hub_judge


@pytest.mark.parametrize("pin", [None, "auto", "local"])
def test_tool_creates_blocked_decision_card_on_hub_without_judge(on_hub, pin):
    """The 11-12 repro: four refusals of a pure decision card from a hub chat."""
    args = {"title": "Decide: keep or drop the nightly digest", "requires": [],
            "initial_status": "blocked", "human_request": dict(ASK),
            **({"placement": pin} if pin is not None else {})}
    result = json.loads(kt._handle_create(args))
    assert result.get("ok"), result
    assert result["status"] == "blocked"
    assert on_hub.calls == []
    with kbc.connect() as conn:
        task = kb.get_task(conn, result["task_id"])
    assert task.status == "blocked"
    assert task.placement_policy == "worker-first-v1"


def test_tool_ready_card_with_hub_wording_is_still_judged(on_hub):
    result = json.loads(kt._handle_create({"title": HUB_TEXT, "requires": []}))
    assert result.get("ok"), result
    assert len(on_hub.calls) == 1


def test_tool_local_pin_refusal_on_hub_names_working_alternative(on_hub):
    result = json.loads(kt._handle_create({"title": HUB_TEXT, "requires": [], "placement": "local"}))
    assert not result.get("ok") and result.get("code") == "placement_unavailable", result
    assert len(on_hub.calls) == 1
    assert "Use placement='local'" not in result["error"]
    assert "omit placement" in result["error"]
    # Following the named alternative works: omitting placement enrolls the card in the pool.
    retried = json.loads(kt._handle_create({"title": HUB_TEXT, "requires": []}))
    assert retried.get("ok"), retried
