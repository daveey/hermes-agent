"""A stored session row whose model disagrees with its recorded route must still resume. [daveey]

Hub chat 20261028_221818_fdf44b: ``sessions.model`` = gpt-6-astra (openai-codex) while ``model_config.model_routing``
and the allocation row recorded an exact pin on claude-opus-5-5 (anthropic). Resume replayed the row's tuple as a NEW
exact pin, the allocator saw the recorded pin outside the single-route candidate set and refused with
"Exact pinned route cannot fail over" on every build.
"""
import json

import pytest

from agent.model_routing import ModelRoutingError, resolve_model_route
from agent.model_routing_managed import pin_policy
from agent.model_routing_restore import restore_route
from agent.model_routing_state import RoutingState
from tests.agent.test_model_routing import policy


def _managed(cfg):
    cfg["model_routing"]["enforce_subscription_only"] = True
    return cfg


def _pin(cfg, model, path, session="s", **kw):
    level, restricted = pin_policy(model, None, cfg)
    return resolve_model_route("class:" + level, config=restricted, session_id=session, state_path=path,
                               exact_pin=True, **kw)


def _recorded(cfg, tmp_path, model):
    path = tmp_path / "routes.db"
    first = _pin(cfg, model, path)
    assert first["metadata"]["exact_pin"] is True
    return path, first["metadata"]


def test_repro_row_model_replayed_as_new_pin_is_refused(tmp_path):
    """Root of the failure: the replayed row tuple collides with the recorded pin."""
    cfg = _managed(policy())
    path, _ = _recorded(cfg, tmp_path, "anthropic-deep")
    with pytest.raises(ModelRoutingError, match="Exact pinned route cannot fail over"):
        _pin(cfg, "openai-codex-deep", path)


def test_resume_restores_the_recorded_pin_when_the_row_disagrees(tmp_path):
    cfg = _managed(policy())
    path, recorded = _recorded(cfg, tmp_path, "anthropic-deep")
    model_config = {"model_routing": recorded, "provider": "openai-codex"}
    restored = restore_route("openai-codex-deep", model_config, policy_config=cfg)
    assert restored == ("anthropic-deep", "anthropic")
    again = _pin(cfg, restored[0], path)
    assert again["metadata"] == recorded  # no reallocation, no write


def test_recorded_class_route_wins_over_a_disagreeing_row(tmp_path):
    cfg = _managed(policy())
    first = resolve_model_route("class:deep", config=cfg, session_id="s", state_path=tmp_path / "r.db")
    model_config = {"model_routing": first["metadata"]}
    other = next(r["model"] for r in cfg["model_routing"]["routes"]["deep"] if r["model"] != first["model"])
    assert restore_route(other, json.dumps(model_config), policy_config=cfg) == ("class:deep", "auto")


def test_legitimate_pin_rejection_survives_the_repair(tmp_path):
    """The recorded pin is gone from policy: it still cannot fail over, and a desynced row does not bypass that."""
    cfg = _managed(policy())
    path, recorded = _recorded(cfg, tmp_path, "anthropic-deep")
    cfg["model_routing"]["routes"]["deep"] = [
        r for r in cfg["model_routing"]["routes"]["deep"] if r["model"] != "anthropic-deep"]
    model_config = {"model_routing": recorded}
    # recorded pin is no longer approved -> restore falls back to the row, which the allocator still refuses
    restored = restore_route("openai-codex-deep", model_config, policy_config=cfg)
    assert restored is None
    with pytest.raises(ModelRoutingError, match="Exact pinned route cannot fail over"):
        _pin(cfg, "openai-codex-deep", path)
    assert RoutingState(path).peek("s", "deep") == recorded


def test_matching_row_is_unchanged(tmp_path):
    cfg = _managed(policy())
    _, recorded = _recorded(cfg, tmp_path, "openai-codex-deep")
    assert restore_route("openai-codex-deep", {"model_routing": recorded}, policy_config=cfg) == (
        "openai-codex-deep", "openai-codex")
