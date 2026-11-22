"""Bounded SPEC77 late-merge and real-construction controls (no provider traffic)."""
import json
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from agent import kanban_delegation_policy as policy
from tests.tools import test_kanban_delegation_no_pin as pins

worker = pins.worker
native = pins.native
effects = pins.effects
interactive = pins.interactive


class Child:
    def __init__(self, overrides):
        self.request_overrides = overrides


@pytest.mark.parametrize("late", [{"model": "late-pin"}, {"extra_body": {"provider": {"order": ["p"]}}},
                                 {"extra_headers": {"HOST": "late.invalid"}}])
def test_postconstruction_tier_merge_rechecked_and_unstarted_child_closed(native, monkeypatch, late):
    import tools.delegate_tool as dt
    routes, error = dt._routes_for([{"goal": "neutral work"}], {}, None, native)
    assert not error
    # Construction can merge provider defaults; the tier pin is the last merge before launch.
    child = Child({"temperature": 0.2})
    policy.register_child(native, child, policy.origin_for(native))
    routes[0].creds["task_tier_overrides"] = late
    closed, detached = [], []
    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", lambda **kw: child)
    monkeypatch.setattr(dt, "_close_child", lambda obj, *a: closed.append(obj))
    monkeypatch.setattr(dt, "_detach_child", lambda parent, obj: detached.append(obj))
    children, error = dt._build_children([{"goal": "neutral work"}], [None], routes, top_role="leaf",
        max_iterations=2, parent_agent=native, live_deleg_id=None, live_writers=[])
    assert children == [] and error, "late routing mutation escaped the actual _build_children caller"
    assert closed == detached == [child]


def test_postconstruction_harmless_tier_merge_admitted(native, monkeypatch):
    import tools.delegate_tool as dt
    routes, error = dt._routes_for([{"goal": "neutral work"}], {}, None, native)
    assert not error
    child = Child({"temperature": 0.2, "service_tier": "old"})
    policy.register_child(native, child, policy.origin_for(native))
    routes[0].creds["task_tier_overrides"] = {"service_tier": "priority"}
    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", lambda **kw: child)
    children, error = dt._build_children([{"goal": "neutral work"}], [None], routes, top_role="leaf",
        max_iterations=2, parent_agent=native, live_deleg_id=None, live_writers=[])
    assert not error and children[0][2] is child
    assert child.request_overrides == {"temperature": 0.2, "service_tier": "priority"}


def test_real_builder_registers_descendants_and_dynamic_alternative_under_pool_wall(native, monkeypatch):
    from agent.credential_pool import load_pool
    from agent.delegation_context import delegated_child_context
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.config import atomic_config_write, load_config_readonly
    from hermes_constants import get_hermes_home
    from tests.agent.test_model_routing import policy as routing_policy
    from tests.hermes_cli.test_codex_subscription_routing import PRO
    import tools.delegate_tool as dt
    home = get_hermes_home()
    # The worker fixture fakes Path.home to its home parent; keep auth's real-root seat belt enabled
    # while distinguishing this disposable HERMES_HOME from that fake account root.
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: home.parent / "account79")
    cfg = load_config_readonly()
    cfg.update(routing_policy())
    alternative = deepcopy(cfg["model_routing"]["routes"]["deep"][0])
    alternative["capabilities"]["streaming"] = True
    blocked = dict(alternative, provider="openai-codex", model="gpt-5.5")
    # Keep the walled route and an authentically excluded paid route in the registry.
    paid = dict(alternative, provider="openrouter", model="paid", billing="metered")
    cfg["model_routing"]["routes"]["deep"] = [blocked, alternative, paid]
    cfg["model"] = {"context_length": 100000}
    atomic_config_write(home / "config.yaml", cfg)
    (home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {
        "openai-codex": [{"id": "wall", "auth_type": "oauth", "source": "manual:device_code",
                          "priority": 0, "access_token": PRO}],
        "anthropic": [{"id": "eligible", "auth_type": "oauth", "source": "manual",
                       "priority": 0, "access_token": "sk-ant-oat01-fixture-only"}]}}))
    pool = load_pool("openai-codex", allowed_auth_types={"oauth"})
    assert pool.has_available(model="gpt-5.5")
    pool.mark_exhausted_and_rotate(status_code=429, credential_id="wall", model="gpt-5.5")
    assert not load_pool("openai-codex", allowed_auth_types={"oauth"}).has_available(model="gpt-5.5")
    raw = "Please delegate neutral analysis using the available subscription class."
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="neutral carrier79", assignee="default")
        kb.add_comment(conn, tid, "operator", raw)
        context = kb.build_worker_context(conn, tid)
    assert raw in context
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kw: [])
    monkeypatch.setattr("agent.anthropic_adapter.build_anthropic_client", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(dt, "_load_config", dict)
    monkeypatch.setattr(dt, "_get_max_spawn_depth", lambda: 4)
    # Local DB-open effect is isolated: allocation and actual AIAgent construction stay real.
    monkeypatch.setattr(dt, "_open_child_session_db", lambda *a, **kw: None)
    monkeypatch.setattr("httpx.Client.send", lambda *a, **kw: pytest.fail("network during construction"))
    made = []
    try:
        for parent in (native,):
            routes, error = dt._routes_for([{"goal": "neutral analysis", "model": "class:deep", "provider": "auto"}], {}, None, parent)
            assert not error
            child = dt._build_child_agent(task_index=0, goal="neutral analysis", context=context, toolsets=None,
                model=routes[0].creds["model"], max_iterations=2, task_count=1, parent_agent=parent,
                logical_request=routes[0].logical, **dt._route_overrides(routes[0]))
            made.append(child)
        with delegated_child_context():
            routes, error = dt._routes_for([{"goal": "neutral descendant"}], {}, None, child)
            assert not error
            grandchild = dt._build_child_agent(task_index=0, goal="neutral descendant", context=None, toolsets=None,
                model=routes[0].creds["model"], max_iterations=2, task_count=1, parent_agent=child,
                logical_request=routes[0].logical, **dt._route_overrides(routes[0]))
            made.append(grandchild)
            rejected, error = dt._routes_for([{"goal": "forged pin", "model": "gpt-5.5"}], {}, None, grandchild)
            assert error and not rejected
        for obj in made:
            assert policy.origin_for(obj) is policy.origin_for(native)
            assert obj.provider == "anthropic" and obj.model == alternative["model"]
            assert obj.model_routing["requested_class"] == "deep"
            assert obj.model_routing["billing_group"] == "subscription"
            assert not getattr(obj, "_native_origin_worker_custody", None)
            assert obj._credential_pool.current().auth_type == "oauth"
            assert not obj._fallback_chain
        assert child.session_id != grandchild.session_id
        assert len(cfg["model_routing"]["routes"]["deep"]) == 3
    finally:
        for obj in reversed(made):
            obj.release_clients()


@pytest.mark.parametrize("sink", ["registry", "dispatch", "facade"])
def test_managed_continue_native_refusal_precedes_effects(native, monkeypatch, sink):
    import tools.delegate_tool as dt
    from tools.registry import registry
    monkeypatch.setattr("tools.delegate_tool_continue._claim_resume", lambda *a: pytest.fail("resume claimed"))
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a: pytest.fail("credentials"))
    args = {"action": "continue", "subagent_id": "old-child", "message": "next"}
    if sink == "registry":
        result = registry.get_entry("delegate_task").handler(args, parent_agent=native)
    elif sink == "dispatch":
        result = native._dispatch_delegate_task(args)
    else:
        result = dt.delegate_task(parent_agent=native, **args)
    assert "unavailable in managed worker sessions" in result


@pytest.mark.parametrize("fields", [{"model": " class:deep"}, {"model": "class:Deep"},
                                   {"model": 7}, {"provider": 7}])
def test_lifecycle_noncanonical_and_nonstring_selectors_have_no_effect(native, effects, fields):
    from agent.subagent_lifecycle import SubagentLifecycleError
    with pytest.raises(SubagentLifecycleError):
        pins._launch(native, **fields)
    assert effects.calls == []


@pins.CALLERS
def test_forged_identity_fields_cannot_clear_authenticated_restriction(native, effects, caller):
    task = {"goal": "do the task", "model": "concrete-pin", "provider": "anthropic",
            "origin": "interactive", "model_source": "restored", "exact_pin": True,
            "metadata": {"origin": "interactive"}, "request_overrides": {"origin": "interactive"}}
    assert pins._refused(caller(native, [task]))
    assert effects.calls == []


@pins.CALLERS
def test_malformed_nonmapping_task_is_structured_error(native, effects, caller):
    assert pins._refused(caller(native, ["not a task"]))
    assert effects.calls == []


def test_unrelated_interactive_object_stays_unrestricted_while_root_registered(native, interactive, effects, monkeypatch):
    assert policy.origin_for(native) is not None and policy.origin_for(interactive) is None
    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", lambda cfg, parent: {
        "model": cfg.get("model"), "provider": cfg.get("provider"), "base_url": None, "api_key": None,
        "api_mode": None, "request_overrides": None})
    monkeypatch.setattr("tools.delegate_tool_config._get_allow_task_model", lambda: True)
    assert pins.ADMITTED in pins._caller_facade(interactive, [{"goal": "do it", "model": "exact"}])
    effects.calls.clear()
    assert pins._refused(pins._caller_facade(native, [{"goal": "do it", "model": "exact"}]))
    assert effects.calls == []


@pytest.mark.parametrize("field", ["pid", "session_id", "store", "agent", "execution_scope"])
def test_malformed_bound_root_capability_refused(worker, field):
    from dataclasses import replace
    agent = worker[0]
    bound = agent._native_origin_worker_custody
    changes = {"pid": -1, "session_id": "wrong-session", "store": object(), "agent": object(),
               "execution_scope": ()}
    agent._native_origin_worker_custody = replace(bound, **{field: changes[field]})
    with pytest.raises(policy.RestrictionError, match="invalid_worker_origin_registration"):
        policy.register_root(agent, agent._test_original_worker_custody)


# ── post-construction recheck: allocator-selected route config is not caller authority ───────

RELAY_URL = "http://127.0.0.1:9/v1"
ROUTE_BODY = {"provider": {"order": ["x"], "allow_fallbacks": False}, "models": ["relay-model"], "route": "fallback"}


def _real_children(native, monkeypatch, *, route_body=None, tier=None, route_headers=None):
    """Real ``_routes_for`` + ``_build_children`` + ``AIAgent``; ``route_body`` None = native subscription route."""
    from pathlib import Path
    from hermes_cli.config import atomic_config_write, load_config_readonly
    from hermes_constants import get_hermes_home
    from tests.agent.test_model_routing import policy as routing_policy
    import tools.delegate_tool as dt
    home = get_hermes_home()
    monkeypatch.setattr(Path, "home", lambda: home.parent / "account79")
    cfg = load_config_readonly()
    cfg.update(routing_policy())
    base = deepcopy(cfg["model_routing"]["routes"]["deep"][0])
    base["capabilities"]["streaming"] = True
    if route_body is None:
        cfg["model_routing"]["routes"]["deep"] = [base]
        (home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"anthropic": [
            {"id": "eligible", "auth_type": "oauth", "source": "manual", "priority": 0,
             "access_token": "-".join(("sk", "ant", "oat01", "fixture", "only"))}]}}))
        monkeypatch.setattr("agent.anthropic_adapter.build_anthropic_client", lambda *a, **kw: MagicMock())
    else:
        cfg["model_routing"]["routes"]["deep"] = [dict(base, provider="custom:relay", model="relay-model",
                                                       billing="metered")]
        cfg["model_routing"]["allow_metered"] = True
        cfg["custom_providers"] = [{"name": "relay", "base_url": RELAY_URL, "api_key": "k", "model": "relay-model",
                                    "extra_body": deepcopy(route_body)}]
        if route_headers:
            cfg["custom_providers"][0]["extra_headers"] = dict(route_headers)
    cfg["model"] = {"context_length": 100000}
    atomic_config_write(home / "config.yaml", cfg)
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kw: [])
    monkeypatch.setattr(dt, "_load_config", dict)
    monkeypatch.setattr(dt, "_get_max_spawn_depth", lambda: 4)
    monkeypatch.setattr(dt, "_open_child_session_db", lambda *a, **kw: None)
    # The relay endpoint is never contacted: stub the local-server probe and keep every other send fatal.
    monkeypatch.setattr("agent.model_metadata.detect_local_server_type", lambda *a, **kw: None)
    monkeypatch.setattr("httpx.Client.send", lambda *a, **kw: pytest.fail("network during construction"))
    closed = []
    real_close = dt._close_child
    monkeypatch.setattr(dt, "_close_child", lambda obj, *a: closed.append(obj) or real_close(obj, *a))
    routes, error = dt._routes_for([{"goal": "neutral analysis", "model": "class:deep"}], {}, None, native)
    assert not error
    if tier is not None:
        routes[0].creds["task_tier_overrides"] = tier
    children, error = dt._build_children([{"goal": "neutral analysis"}], [None], routes, top_role="leaf",
        max_iterations=2, parent_agent=native, live_deleg_id=None, live_writers=[])
    return [c for _, _, c in children], error, closed


@pytest.mark.parametrize("route_body", [ROUTE_BODY, {"model": "upstream-alias"}, {"service_tier": "flex"}, None],
                         ids=["routing-keys", "model-key", "control-tier", "native"])
def test_real_build_children_admits_the_allocated_routes_own_extra_body(native, monkeypatch, route_body):
    children, error, closed = _real_children(native, monkeypatch, route_body=route_body)
    try:
        assert not error, error
        child, = children
        assert closed == [] and policy.origin_for(child) is policy.origin_for(native)
        if route_body is None:
            assert child.provider == "anthropic" and not (child.request_overrides or {}).get("extra_body")
        else:  # the operator's route config reaches the wire unchanged; nothing was stripped to pass the check
            assert child.provider == "custom:relay" and child.model == "relay-model"
            assert child.request_overrides == {"extra_body": route_body}
    finally:
        for child in children:
            child.release_clients()


@pytest.mark.parametrize("tier", [
    {"extra_body": deepcopy(ROUTE_BODY)},                       # spoof: same keys AND values as the trusted route
    {"extra_body": {"provider": ROUTE_BODY["provider"]}},       # same-key collision, equal value
    {"extra_body": {"provider": {"only": ["other"]}}},          # same key, different value
    {"model": "late-pin"},
], ids=["spoof-equal", "collision-equal", "collision-differs", "top-level"])
def test_real_build_children_refuses_tier_overlay_routing_keys_even_when_equal_to_route_config(
        native, monkeypatch, tier):
    children, error, closed = _real_children(native, monkeypatch, route_body=ROUTE_BODY, tier=tier)
    assert children == [] and error and "routing override" in error
    assert len(closed) == 1 and closed[0].provider == "custom:relay"  # built, refused before launch, closed


@pytest.mark.parametrize("overrides", [{"extra_body": {"provider": ROUTE_BODY["provider"]}},
                                       {"extra_body": {"route": "fallback"}}])
def test_caller_overlay_equal_to_route_config_is_refused_before_construction(native, monkeypatch, overrides):
    import tools.delegate_tool as dt
    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", lambda **kw: pytest.fail("constructed"))
    routes, error = dt._routes_for([{"goal": "neutral analysis", "model": "class:deep"}],
                                   {"request_overrides": overrides}, None, native)
    assert routes == [] and error and "routing override" in error


def _built(final, *, caller=None, providers=None):
    from types import SimpleNamespace
    return SimpleNamespace(provider="custom:relay", model="relay-model", base_url=RELAY_URL, request_overrides=final,
                           _caller_extra_body=caller or {}, _custom_providers=providers if providers is not None else [
                               {"name": "relay", "base_url": RELAY_URL, "model": "relay-model",
                                "extra_body": deepcopy(ROUTE_BODY)}])


def test_built_child_check_admits_only_the_trusted_routes_exact_extra_body():
    policy.check_built_child(_built({"temperature": 0.1, "extra_body": {**ROUTE_BODY, "service_tier": "flex"}}))
    policy.check_built_child(_built({"extra_body": {"service_tier": "flex"}}, providers=[]))
    policy.check_built_child(_built(None))


@pytest.mark.parametrize("child", [
    _built({"extra_body": deepcopy(ROUTE_BODY)}, caller={"provider": ROUTE_BODY["provider"]}),  # caller-owned, equal
    _built({"extra_body": deepcopy(ROUTE_BODY)}, caller={"Provider": {"order": ["x"]}}),
    _built({"extra_body": {**ROUTE_BODY, "provider": {"order": ["y"]}}}),                # route value was replaced
    _built({"extra_body": {**ROUTE_BODY, "fallbacks": ["m"]}}),                          # key the route never set
    _built({"extra_body": deepcopy(ROUTE_BODY)}, providers=[]),                          # no trusted source at all
    _built({"extra_body": deepcopy(ROUTE_BODY)},
           providers=[{"name": "other", "base_url": RELAY_URL, "extra_body": deepcopy(ROUTE_BODY)}]),
    _built({"extra_body": deepcopy(ROUTE_BODY), "model": "x"}),
    _built({"extra_body": deepcopy(ROUTE_BODY), "extra_query": {"model": "x"}}),
    _built({"extra_body": deepcopy(ROUTE_BODY), "extra_headers": {"Authorization": "Bearer x"}}),
    _built({"extra_body": ["provider"]}),
    _built({"extra_body": deepcopy(ROUTE_BODY)}, caller=["provider"]),
], ids=["caller-equal", "caller-case", "replaced", "added", "no-route", "other-route", "top-level", "query",
        "header", "body-shape", "caller-shape"])
def test_built_child_check_refuses_unauthorized_or_caller_owned_routing_keys(child):
    with pytest.raises(policy.RestrictionError):
        policy.check_built_child(child)


def test_built_child_check_refuses_tier_routing_keys_before_reading_the_route():
    child = _built({"extra_body": deepcopy(ROUTE_BODY)})
    with pytest.raises(policy.RestrictionError):
        policy.check_built_child(child, {"extra_body": {"provider": ROUTE_BODY["provider"]}})


# ── finite destination/auth names on the caller/tier override surface (placeholder values only) ──

AUTH_NAMES = [("extra_headers", name) for name in (
    "x-goog-api-key", "X-Goog-Api-Key", "Cookie", "COOKIE", "Proxy-Authorization", "proxy-AUTHORIZATION",
    "OpenAI-Organization", "openai-organization")] + [("extra_query", "key"), ("extra_query", "api-key"),
                                                      ("extra_query", "Api-Key")]
AUTH_IDS = [f"{c}.{n}" for c, n in AUTH_NAMES]
ROUTE_HEADERS = {"x-goog-api-key": "placeholder", "Cookie": "placeholder", "Proxy-Authorization": "placeholder",
                 "OpenAI-Organization": "placeholder"}


@pytest.mark.parametrize("container,name", AUTH_NAMES, ids=AUTH_IDS)
def test_auth_names_refused_for_caller_before_construction(native, monkeypatch, container, name):
    import tools.delegate_tool as dt
    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", lambda **kw: pytest.fail("constructed"))
    routes, error = dt._routes_for([{"goal": "neutral analysis", "model": "class:deep"}],
                                   {"request_overrides": {container: {name: "placeholder"}}}, None, native)
    assert routes == [] and error and "destination/auth override" in error


@pytest.mark.parametrize("container,name", AUTH_NAMES, ids=AUTH_IDS)
def test_real_build_children_refuses_tier_auth_names_even_when_the_route_sets_the_same_header(
        native, monkeypatch, container, name):
    """Same-key collision: the allocated route's own ``extra_headers`` carry the name with the same value."""
    children, error, closed = _real_children(native, monkeypatch, route_body={"service_tier": "flex"},
                                             route_headers=ROUTE_HEADERS, tier={container: {name: "placeholder"}})
    assert children == [] and error and "destination/auth override" in error
    assert len(closed) == 1 and closed[0].provider == "custom:relay"  # built, refused before launch, closed


@pytest.mark.parametrize("tier", [None, {"extra_headers": {"x-trace-id": "t"}, "extra_query": {"trace": "t"}}],
                         ids=["route-headers-only", "harmless-tier"])
def test_real_build_children_keeps_the_allocated_routes_own_auth_headers(native, monkeypatch, tier):
    """Allocator-owned route headers live on the client, not in delegation overrides, and are not refused."""
    children, error, closed = _real_children(native, monkeypatch, route_body={"service_tier": "flex"},
                                             route_headers=ROUTE_HEADERS, tier=tier)
    try:
        assert not error, error
        child, = children
        assert closed == [] and child.provider == "custom:relay"
        if tier is None:
            assert child.request_overrides.get("extra_body") == {"service_tier": "flex"}
        else:  # harmless tier tuning passes through untouched
            assert child.request_overrides["extra_headers"] == tier["extra_headers"]
            assert child.request_overrides["extra_query"] == tier["extra_query"]
        for name in ROUTE_HEADERS:
            assert name not in (child.request_overrides.get("extra_headers") or {})
    finally:
        for child in children:
            child.release_clients()


@pytest.mark.parametrize("container,name", AUTH_NAMES, ids=AUTH_IDS)
def test_built_child_check_refuses_auth_names_in_the_final_mapping(container, name):
    with pytest.raises(policy.RestrictionError, match="destination/auth override"):
        policy.check_built_child(_built({"extra_body": deepcopy(ROUTE_BODY), container: {name: "placeholder"}}))
