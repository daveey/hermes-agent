"""Mandatory pins are rejected at ingress; historical marks cannot constrain placement."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_mandatory_pin as mp
from hermes_cli import kanban_pin_reroute as pr
from hermes_cli import kanban_placement as kp
from tests.hermes_cli import test_kanban_model_tier as fixtures

board = fixtures.board


@pytest.mark.parametrize("model,provider", [(None, None), ("class:deep", "auto"),
                                               ("claude-opus-5-5", "anthropic")])
def test_new_mandatory_pin_is_rejected_without_creating_a_card(board, model, provider):
    with kbc.connect() as conn:
        before = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
        with pytest.raises(ValueError, match="model-neutral"):
            kb.create_task(conn, title="work", assignee="default", model_override=model,
                           provider_override=provider, pin_mandatory=True)
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == before


@pytest.mark.parametrize("model", [None, "class:deep", "claude-opus-5-5"])
def test_later_mandatory_mark_is_rejected_without_mutation(board, model):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="work", assignee="default")
        # Only this fixture DB receives a historical concrete selector.
        conn.execute("UPDATE tasks SET model_override=? WHERE id=?", (model, tid))
        conn.commit()
        before = [e.kind for e in kb.list_events(conn, tid)]
        with pytest.raises(ValueError, match="model-neutral"):
            pr.set_pin_mandatory(conn, tid, True)
        stored = kb.get_task(conn, tid)
        assert stored is not None
        assert stored.model_override == model and stored.model_pin_mandatory is None
        assert [e.kind for e in kb.list_events(conn, tid)] == before


@pytest.mark.parametrize("card", [
    {"model_override": "claude-opus-5-5", "provider_override": "openai-codex", "model_pin_mandatory": 1},
    {"model_override": "class:deep", "model_pin_mandatory": 1},
    {"model_override": None, "model_pin_mandatory": 1},
    {"model": "claude-opus-5-5", "provider": "anthropic", "pin_mandatory": True},
])
def test_legacy_mark_imposes_no_model_or_attestation_requirement(card):
    assert mp.pin_of(card) is None
    assert mp.requirements(card) == set()
    assert mp.refusal({"id": "t", "assignee": "nonexistent", **card}) is None
    assert not pr.is_mandatory_pin(type("Task", (), card)())
    for placement in (["pool:worker"], ["machine:worker"], ["pool:worker", "machine:hub"]):
        assert mp.carry(placement, card, me="hub") == placement


def test_legacy_pin_does_not_fence_worker_claim(monkeypatch):
    from hermes_cli import kanban_placement_coordination as coord
    from hermes_cli import kanban_worker_node as wn
    import time

    machines = {"w": {"state": "active", "pickup": "true", "last_seen": time.time() * 1000,
                      "labels": ["pool:worker"]}}
    monkeypatch.setattr(wn, "registry", lambda: {"machines": machines})
    card = {"title": "work", "placement_policy": "worker-first-v1",
            "model_override": "claude-opus-5-5", "model_pin_mandatory": 1}
    assert coord.claim_refusal(card, "w") is None


def test_legacy_pin_does_not_add_a_pickup_label(monkeypatch):
    from hermes_cli import kanban_hub_admission_pickup as hap
    monkeypatch.setattr(kp, "structural_reason", lambda args, cfg=None: None)
    monkeypatch.setattr(kp, "portable_reason", lambda args, cfg=None: None)
    monkeypatch.setattr(kp, "_machine_picks_up", lambda label: True)
    card = {"title": "work", "body": "", "assignee": "default", "workspace_kind": "scratch",
            "requires": "[]", "skills": None, "model_override": "claude-opus-5-5",
            "provider_override": "anthropic", "model_pin_mandatory": 1}
    assert hap._target(hap.card_fields(card, "board"), "hub", {"worker_queue": {"hub": "pool:worker"}}, {}) == ["pool:worker"]


def test_courier_does_not_transport_new_mandatory_directives():
    assert kp._uncarried({"title": "work", "pin_mandatory": True}, kp._Cfg({}), True) == ["pin_mandatory"]
