"""A class tier this host cannot route must not crash the worker at startup [daveey].

2026-10-27: shared config switched ``kanban.default_model`` to ``class:standard`` on the MacBook while its
``model_routing`` had ``enabled: true`` and no routes; the dispatched worker exited 1 with "No eligible
standard routes under billing/capability policy". The dispatcher now keeps the profile's own model when the
tier-chosen class has no eligible route on this host, and records why on the ``model_tier`` event.

Entry paths: a real dispatch tick (config read from HERMES_HOME), and the strong (class:deep) tier for a
review-lane card.
"""
from __future__ import annotations

import pytest
import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_model_tier as tier
from tests.hermes_cli import test_kanban_model_tier

board = test_kanban_model_tier.board

ROUTE = {"provider": "anthropic", "model": "claude-sonnet-5-5", "billing": "subscription", "weight": 1,
         "eligible": True, "funding_verified": True,
         "capabilities": {"tools": True, "streaming": True, "context_tokens": 1000000}}


def _write(home, routing, providers=None):
    cfg = {"kanban": {"default_model": "class:standard", "default_provider": "auto"}, "model_routing": routing}
    if providers is not None:
        cfg["providers"] = providers
    # JSON is valid YAML (the test venv has no PyYAML).
    home.joinpath("config.yaml").write_text(json.dumps(cfg), encoding="utf-8")


def _tick(monkeypatch):
    """One dispatch tick: {task_id: (in-memory override, (model, provider) the owner runs)}.

    Fork: upstream moved card execution to the profile owner (the worker argv is now just
    ``-m hermes_cli.kanban_worker_client``), so the model the worker runs is the owner's
    ``owner_model_pin`` of the re-read card, not ``-m``/``--provider`` in the argv."""
    seen = {}
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine", assignee="default")
        dispatch.dispatch_once(conn, dry_run=False, spawn_fn=lambda task, ws: seen.update(
            {task.id: (task.model_override, tier.owner_model_pin(conn, kb.get_task(conn, task.id)))}))
        events = [e.payload for e in kb.list_events(conn, tid) if e.kind == "model_tier"]
    return seen[tid], events


@pytest.mark.parametrize("routing, providers, why", [
    ({"enabled": True}, None, "no_eligible_routes"),  # the incident: enabled, registry not landed yet
    ({"enabled": True, "routes": {"standard": [dict(ROUTE, eligible=False)]}}, None, "no_eligible_routes"),
    ({"enabled": True, "routes": {"standard": [ROUTE]}}, {"anthropic": {"enabled": False}}, "no_eligible_routes"),
    ({"enabled": False, "routes": {"standard": [ROUTE]}}, None, "routing_disabled"),
])
def test_unroutable_class_tier_keeps_profile_model(board, monkeypatch, routing, providers, why):
    _write(board, routing, providers)
    (override, pin), events = _tick(monkeypatch)
    assert override is None
    assert pin == (None, None)  # the worker runs on its profile's model, not a dead class
    assert events[-1]["reason"] == f"default+unroutable:class:standard:{why}"
    assert events[-1]["model"] is None


def test_routable_class_tier_is_kept(board, monkeypatch):
    _write(board, {"enabled": True, "routes": {"standard": [ROUTE]}})
    (override, pin), events = _tick(monkeypatch)
    assert override == "class:standard"
    assert pin == ("class:standard", "auto")
    assert events[-1]["reason"] == "default"


def test_review_with_only_eligible_standard_route_uses_tier_fallback(board):
    """The review resolver may use its eligible standard fallback without author-model history."""
    cfg = {"enabled": True, "routes": {"standard": [ROUTE]}}
    _write(board, cfg)
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="impl", assignee="default"))
        reason = tier.apply_model_tier(conn, task, lane="review", cfg={"default_model": "class:standard"})
    assert reason == "review_tier_fallback"
    assert task.model_override == ROUTE['model']


def test_card_pinned_class_is_left_to_the_card(board):
    """An explicit card pin is the creator's choice; the tier fallback does not rewrite it."""
    _write(board, {"enabled": True})
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="x", assignee="default", model_override="class:deep",
                                                provider_override="auto"))
        reason = tier.apply_model_tier(conn, task, lane="ready")
    assert reason == "pinned"
    assert task.model_override == "class:deep"
