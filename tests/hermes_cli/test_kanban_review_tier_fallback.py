"""Standard-tier review is available when eligible deep pools are unavailable."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_llm_pacing_dispatch as pacing
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_provider_walls as walls
from hermes_cli import kanban_review_model as review
from tests.hermes_cli import test_kanban_review_model as helpers

board = helpers.board
DEEP = ["claude-fable-5-2", "claude-opus-5-5", "gpt-6-astra", "grok-4.7"]
STANDARD = "gpt-6.1-sol"


def fallback_config(board):
    cfg = helpers.config(board, strong="class:deep", routes=DEEP)
    path = board / "config.yaml"
    data = json.loads(path.read_text(encoding="utf-8"))
    template = data["model_routing"]["routes"]["deep"][0]
    data["model_routing"]["routes"]["standard"] = [
        {**template, "model": STANDARD, "provider": "openai-codex"}]
    path.write_text(json.dumps(data), encoding="utf-8")
    return cfg


def author_task(conn):
    t = helpers.task(conn, DEEP[0])
    from tests.hermes_cli import test_kanban_review_implementer_set as authors
    for model in DEEP[1:]:
        authors._run(conn, t.id, model=model)
    return kb.get_task(conn, t.id)


def test_held_deep_authors_fall_back_to_standard(board, monkeypatch):
    cfg = fallback_config(board)
    monkeypatch.setattr(pacing, "held_pools", lambda *a: {"anthropic": 9999999999})
    with kbc.connect() as conn:
        t = author_task(conn)
        assert review.implementer_models(conn, t) == set(DEEP)
        assert review.resolve_review_model(conn, t, cfg) == (
            STANDARD, "openai-codex", "review_tier_fallback")


@pytest.mark.parametrize("blocked", ["authored", "walled", "held", "live_wall"])
def test_standard_availability_ignores_authorship_but_respects_pool_blocks(board, monkeypatch, blocked):
    cfg = fallback_config(board)
    monkeypatch.setattr(pacing, "held_pools", lambda *a: {"anthropic": 9999999999})
    if blocked == "walled":
        monkeypatch.setattr(walls, "active_walls", lambda **kw: {"openai-codex": 9999999999})
    if blocked == "held":
        monkeypatch.setattr(pacing, "held_pools", lambda *a: {"anthropic": 9999999999, "openai-codex": 9999999999})
    if blocked == "live_wall":
        monkeypatch.setattr(walls, "live_quota_wall", lambda p, m: m == STANDARD)
    with kbc.connect() as conn:
        t = author_task(conn)
        if blocked == "authored":
            from tests.hermes_cli import test_kanban_review_implementer_set as authors
            authors._run(conn, t.id, model=STANDARD)
            assert review.resolve_review_model(conn, t, cfg) == (
                STANDARD, "openai-codex", "review_tier_fallback")
            return
        with pytest.raises(review.ReviewModelUnavailable):
            review.resolve_review_model(conn, t, cfg)


def test_legacy_mandatory_pin_is_audit_only_and_author_model_is_eligible(board):
    cfg = fallback_config(board)
    with kbc.connect() as conn:
        author = author_task(conn)
        tid = kb.create_task(conn, title="Independent review of fixture", assignee="default",
                             creator_task_id=author.id)
        conn.execute("UPDATE tasks SET model_override=?, provider_override='anthropic' WHERE id=?", (DEEP[0], tid))
        conn.execute("UPDATE tasks SET model_pin_mandatory=1 WHERE id=?", (tid,))
        conn.commit()
        assert review.resolve_review_model(conn, kb.get_task(conn, tid), cfg) == (
            DEEP[0], "anthropic", "review_unpinned")
        assert kb.get_task(conn, tid).model_pin_mandatory == 1


@pytest.mark.parametrize("blocked", ["authored", "walled", "held", "live_wall", "ineligible"])
def test_standard_policy_order_skips_unavailable_routes(board, monkeypatch, blocked):
    cfg = fallback_config(board)
    monkeypatch.setattr(pacing, "held_pools", lambda *a: {"anthropic": 9999999999})
    path = board / "config.yaml"
    data = json.loads(path.read_text(encoding="utf-8"))
    routes = data["model_routing"]["routes"]["standard"]
    bad = {**routes[0], "model": "grok-standard", "provider": "xai-oauth"}
    if blocked == "ineligible":
        bad["eligible"] = False
    routes.insert(0, bad)
    routes.append({**routes[1], "model": "gpt-later"})
    path.write_text(json.dumps(data), encoding="utf-8")
    if blocked == "walled":
        monkeypatch.setattr(walls, "active_walls", lambda **kw: {"xai-oauth": 9999999999})
    if blocked == "held":
        monkeypatch.setattr(pacing, "held_pools", lambda *a: {"anthropic": 9999999999, "xai-oauth": 9999999999})
    if blocked == "live_wall":
        monkeypatch.setattr(walls, "live_quota_wall", lambda p, m: m == bad["model"])
    with kbc.connect() as conn:
        t = author_task(conn)
        if blocked == "authored":
            from tests.hermes_cli import test_kanban_review_implementer_set as authors
            authors._run(conn, t.id, model=bad["model"])
            assert review.resolve_review_model(conn, t, cfg) == (
                bad["model"], bad["provider"], "review_tier_fallback")
            return
        assert review.resolve_review_model(conn, t, cfg) == (
            STANDARD, "openai-codex", "review_tier_fallback")


@pytest.mark.parametrize("blocked", ["none", "walled", "held"])
def test_deep_preferred_unless_its_pool_unavailable(board, monkeypatch, blocked):
    cfg = fallback_config(board)
    if blocked == "walled":
        monkeypatch.setattr(walls, "active_walls", lambda **kw: {"anthropic": 9999999999})
    if blocked == "held":
        monkeypatch.setattr(pacing, "held_pools", lambda *a: {"anthropic": 9999999999})
    with kbc.connect() as conn:
        t = helpers.task(conn, DEEP[0])
        expected = ((DEEP[0], "anthropic", "review_unpinned") if blocked == "none" else
                    (STANDARD, "openai-codex", "review_tier_fallback"))
        assert review.resolve_review_model(conn, t, cfg) == expected


def test_dispatch_records_fallback_in_both_route_receipts(board, monkeypatch):
    fallback_config(board)
    monkeypatch.setattr(pacing, "held_pools", lambda *a: {"anthropic": 9999999999})
    seen = []
    with kbc.connect() as conn:
        t = author_task(conn)
        kb.claim_task(conn, t.id)
        assert kb.request_review(conn, t.id)
        dispatch.dispatch_once(conn, dry_run=False,
                               spawn_fn=lambda item, ws: seen.append(item.model_override))
        stored = kb.get_task(conn, t.id)
        assert seen == [STANDARD]
        assert stored.model_override == DEEP[0]
        assert tier.owner_model_pin(conn, stored) == (STANDARD, "openai-codex")
        events = {e.kind: e.payload for e in kb.list_events(conn, t.id)
                  if e.run_id == stored.current_run_id and e.kind in ("model_tier", "run_route")}
        assert events["model_tier"]["reason"] == "review_tier_fallback"
        assert events["run_route"]["tier_reason"] == "review_tier_fallback"


def test_refused_override_retains_deep_only_recovery(board, monkeypatch):
    from types import SimpleNamespace
    cfg = fallback_config(board)
    refusal = SimpleNamespace(code="model_pin_not_in_registry", allowed=[STANDARD])
    monkeypatch.setattr(review, "registry_refusal",
                        lambda m, p, a: None if m == STANDARD else refusal)
    with kbc.connect() as conn:
        t = author_task(conn)
        kb.claim_task(conn, t.id)
        assert kb.request_review(conn, t.id)
        conn.execute("UPDATE task_runs SET metadata=? WHERE task_id=? AND outcome='review_requested'",
                     ('{"review_model": "claude-refused"}', t.id))
        assert review.resolve_review_model(conn, t, cfg) == (
            STANDARD, "openai-codex", "review_tier_fallback")
