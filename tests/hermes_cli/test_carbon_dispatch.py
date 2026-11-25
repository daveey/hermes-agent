"""Explicit carbon cannot leak into default, review, retry or reroute tiers."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_model_tier as tier
from tests.agent.test_carbon_routing import carbon_config
from tests.hermes_cli import test_kanban_strong_tier as helpers

board = helpers.board


def task(model=None):
    return SimpleNamespace(id="card", assignee="default", model_override=model, provider_override=None,
                           skills=[], title="routine", body="", current_run_id=None)


@pytest.mark.parametrize("case", ["default", "retry", "pattern", "review"])
def test_carbon_cannot_be_configured_as_implicit_tier(monkeypatch, case):
    from hermes_cli import kanban_review_model as review
    monkeypatch.setattr(review, "is_review_run", lambda *a: False)
    monkeypatch.setattr(tier, "_prior_failure", lambda *a: "run_crashed" if case == "retry" else None)
    cfg = {"default_model": "class:carbon", "strong_model": "class:carbon",
           "default_model_keep_patterns": ["routine"] if case == "pattern" else []}
    result = tier.resolve_model_tier(None, task(), lane="review" if case == "review" else "ready", cfg=cfg)
    assert result[:2] == ("class:standard" if case == "default" else "class:deep", "auto")


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_explicit_card_carbon_survives_tiering(monkeypatch, lane):
    from hermes_cli import kanban_review_model as review
    monkeypatch.setattr('hermes_cli.kanban_refusal_fallback.refused_providers', lambda *a: [])  # no DB in this unit
    monkeypatch.setattr(review, "_override", lambda *a: None)
    assert tier.resolve_model_tier(None, task("class:carbon"), lane=lane, cfg={}) == (
        "class:carbon", "auto", "class")
    from hermes_cli import kanban_quota_episode as episode
    monkeypatch.setattr(episode, "decision", lambda *a, **k: pytest.fail("carbon entered quota reroute"))
    assert tier._route_from_base(None, task("class:carbon"), ("class:carbon", "auto", "class"),
                                 lane=lane, cfg={}) == ("class:carbon", "auto", "class")


def test_automatic_review_and_ratio_and_peer_candidates_exclude_carbon(monkeypatch):
    from hermes_cli import kanban_model_assignment as assignment
    from hermes_cli import kanban_ratio_routing as ratio
    from hermes_cli import kanban_review_model as review
    from hermes_cli import kanban_pin_reroute as pin
    monkeypatch.setattr(assignment, "_assignee_config", lambda *a: carbon_config())
    for level in ("standard", "deep", "fast"):
        assert all(provider != "gemini" for _, provider in review.independent_routes(set(), {}, "default", level=level))
    for selector in ("class:carbon", "gemini-4-carbon"):
        assert ratio._pool_routes(task(), selector) == []
    assert pin.peer_route(task(), "gemini-4-carbon", {}) is None


def test_explicit_review_handoff_carbon_is_honored_without_deep_fallback(monkeypatch):
    from hermes_cli import kanban_model_assignment as assignment
    monkeypatch.setattr('hermes_cli.kanban_refusal_fallback.refused_providers', lambda *a: [])  # no DB in this unit
    from hermes_cli import kanban_review_model as review
    monkeypatch.setattr(assignment, "_assignee_config", lambda *a: carbon_config())
    monkeypatch.setattr(review, "_override", lambda *a: ("class:carbon", "auto"))
    monkeypatch.setattr(review, "registry_refusal", lambda *a: None)
    assert review.resolve_review_model(None, task(), {}) == ("class:carbon", "auto", "review_override")


def test_no_strong_fallback_or_episode_alternate_to_carbon(monkeypatch):
    from hermes_cli import kanban_quota_episode as episode
    from hermes_cli import kanban_model_neutral as neutral
    monkeypatch.setattr(neutral, "routing_view", lambda t: t)
    assert episode._strong_fallback(task(), ("class:standard", "auto", "default"), {},
                                    {"strong_model": "class:carbon"}, 0) is None
    from hermes_cli import kanban_model_assignment as assignment
    monkeypatch.setattr(assignment, "_assignee_config", lambda *a: carbon_config())
    assert episode.alternate_route(None, task(), {"gemini": 100}, ("class:carbon", "auto", "class"), 0) is None
    assert episode.alternate_route(None, task(), {"gemini": 100}, ("gemini-4-carbon", "gemini", "default"), 0) is None


@pytest.mark.parametrize("handoff", ["class:deep", "class:carbon", None])
def test_carbon_card_review_handoff_precedence(board, monkeypatch, handoff):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_model_assignment as assignment

    config = carbon_config()
    deep = config["model_routing"]["routes"]["deep"][1]
    deep["capabilities"]["streaming"] = True
    config["model_routing"]["routes"]["deep"] = [deep]
    monkeypatch.setattr(assignment, "_assignee_config", lambda *a: config)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="carbon review fixture", assignee="default",
                             model_override="class:carbon", provider_override="auto")
        assert kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, metadata={"review_model": handoff} if handoff else {})
        assert kb.claim_review_task(conn, tid)
        result = tier.resolve_model_tier(conn, kb.get_task(conn, tid), lane="review", cfg={})
        if handoff == "class:deep":
            assert result == (deep["model"], "openai-codex", "review_override")
        else:
            assert result == ("class:carbon", "auto", "review_override" if handoff else "class")


def test_dashboard_exposes_explicit_carbon():
    from plugins.kanban.dashboard.plugin_api import model_options
    assert "class:carbon" in model_options()["providers"][0]["models"]
