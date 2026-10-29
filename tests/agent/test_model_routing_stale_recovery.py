"""Recovery from stale sticky selections never weakens exact pins or preparation."""
import sqlite3

import pytest

from agent.model_routing import ModelRoutingError, resolve_model_route
from agent.model_routing_state import RoutingState
from tests.agent.test_model_routing import policy


@pytest.mark.parametrize("cause", ["removed", "capability", "unavailable"])
@pytest.mark.parametrize("stored_pin,current_pin", [(False, False), (True, False), (False, True)])
def test_stale_route_recovery_and_exact_pin_fence(tmp_path, cause, stored_pin, current_pin):
    cfg = policy()
    path = tmp_path / "routes.db"
    args = dict(config=cfg, session_id="original", state_path=path)
    first = resolve_model_route("class:deep", exact_pin=stored_pin, **args)
    state = RoutingState(path)
    state.link_continuation("child", "original")
    routes = cfg["model_routing"]["routes"]["deep"]
    selected = next(r for r in routes if r["model"] == first["model"])
    kwargs = {}
    if cause == "removed":
        routes.remove(selected)
    elif cause == "capability":
        selected["capabilities"]["vision"] = False
        kwargs["requirements"] = {"vision": True}
    else:
        kwargs["eligible"] = lambda r: r["model"] != first["model"]
    if stored_pin or current_pin:
        with pytest.raises(ModelRoutingError, match="Exact pinned route"):
            resolve_model_route("class:deep", exact_pin=current_pin, **kwargs, **args)
        assert state.peek("original", "deep") == first["metadata"]
    else:
        new = resolve_model_route("class:deep", **kwargs, **args)
        assert new["model"] != first["model"]
        assert new["metadata"]["requested_class"] == "deep"
        assert new["metadata"]["reason"] == "same-class-failover"
        assert new["metadata"]["billing_group"] == "subscription"
        assert state.peek("child", "deep") == new["metadata"]
        assert resolve_model_route("class:deep", **kwargs, **args) == new


@pytest.mark.parametrize("prepare_fails", [False, True])
def test_automatic_recovery_is_transactional(tmp_path, prepare_fails):
    cfg = policy()
    path = tmp_path / "routes.db"
    args = dict(config=cfg, session_id="original", state_path=path)
    first = resolve_model_route("class:deep", **args)
    state = RoutingState(path)
    with sqlite3.connect(path) as db:
        counters = db.execute("SELECT * FROM counters ORDER BY pool").fetchall()
    cfg["model_routing"]["routes"]["deep"] = [
        r for r in cfg["model_routing"]["routes"]["deep"] if r["model"] != first["model"]]

    def prepare(metadata):
        assert metadata["selected_model"] != first["model"]
        assert state.peek("original", "deep") == first["metadata"]
        if prepare_fails:
            raise RuntimeError("unready client")

    if prepare_fails:
        with pytest.raises(RuntimeError, match="unready client"):
            resolve_model_route("class:deep", prepare=prepare, **args)
        assert state.peek("original", "deep") == first["metadata"]
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT * FROM counters ORDER BY pool").fetchall() == counters
    else:
        result = resolve_model_route("class:deep", prepare=prepare, **args)
        assert state.peek("original", "deep") == result["metadata"]


def test_no_same_class_alternative_does_not_cross_tiers(tmp_path):
    cfg = policy()
    path = tmp_path / "routes.db"
    args = dict(config=cfg, session_id="original", state_path=path)
    first = resolve_model_route("class:deep", **args)
    cfg["model_routing"]["routes"]["deep"] = []
    with pytest.raises(ModelRoutingError, match="No eligible deep routes"):
        resolve_model_route("class:deep", **args)
    assert RoutingState(path).peek("original", "deep") == first["metadata"]
