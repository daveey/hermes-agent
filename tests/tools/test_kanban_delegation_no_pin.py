"""New Kanban-origin delegations take a logical model class, never a concrete model/provider pin.

Every test drives a real named caller (registry handler, AIAgent dispatch, delegate_task/_routes_for,
SubagentLifecycleService.launch, the child builder) from the authenticated adopted-worker fixture, with
*effect* sentinels around credential resolution, budget, transcripts, announcements, builds and
executor submission.  A positive control stops at the first allowed effect: admission proof, not a model run.
"""
import json
from types import SimpleNamespace

import pytest

from tests.hermes_cli import test_origin_worker_custody as custody_tests

worker = custody_tests.worker  # module alias: a directly imported fixture name would be shadowed (F811)

CLASSES = ("deep", "standard", "fast")
ADMITTED = "admitted-at-build"


def _register(agent, capability):
    from agent import kanban_delegation_policy as policy
    policy.register_root(agent, capability)


class Effects:
    def __init__(self):
        self.calls = []
        self.routes = []
        self.build_kwargs = {}

    def note(self, name):
        self.calls.append(name)


@pytest.fixture
def effects(monkeypatch):
    rec = Effects()
    import tools.delegate_tool as dt
    from agent import subagent_lifecycle as lifecycle
    import tools.delegation_live_log as live_log
    real_resolve = dt._resolve_delegation_credentials

    def resolver(cfg, parent):
        rec.note("credential_resolver")
        return real_resolve(cfg, parent)

    def build_children(task_list, task_schemas, routes, **kw):
        rec.note("build")
        rec.routes = routes
        return [], ADMITTED

    def preserving(**kw):
        rec.note("lifecycle_build")
        rec.build_kwargs = kw
        return SimpleNamespace(_subagent_id="sa-0-fixture", _delegate_role="leaf", _delegate_depth=1,
                               provider="auto", model=kw.get("model"))

    monkeypatch.setattr(dt, "_resolve_delegation_credentials", resolver)
    monkeypatch.setattr(dt, "_build_children", build_children)
    monkeypatch.setattr(dt, "_oneshot_spawn_budget", lambda *a, **k: rec.note("budget"))
    monkeypatch.setattr(dt, "_announce_batch", lambda *a, **k: rec.note("announce"))
    monkeypatch.setattr(dt, "_open_child_session_db", lambda *a, **k: rec.note("child_session"))
    monkeypatch.setattr(live_log, "create_live_transcripts",
                        lambda *a, **k: rec.note("transcript") or (None, [], []))
    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", preserving)
    monkeypatch.setattr("tools.delegate_tool_results._build_child_preserving_parent_tools", preserving)
    monkeypatch.setattr(lifecycle._EXECUTOR, "submit", lambda *a, **k: rec.note("executor") or SimpleNamespace())
    real_cleanup = lifecycle.SubagentLifecycleService._cleanup_locked
    monkeypatch.setattr(lifecycle.SubagentLifecycleService, "_cleanup_locked",
                        lambda self: rec.note("lifecycle_cleanup") or real_cleanup())
    monkeypatch.setattr(dt, "_load_config", dict)
    monkeypatch.setattr(dt, "is_spawn_paused", lambda: False)
    return rec


@pytest.fixture
def native(worker):
    agent, _store, _db, _context, _frame = worker
    _register(agent, agent._test_original_worker_custody)
    agent.model_routing = {"requested_class": "deep"}
    return agent


@pytest.fixture
def interactive():
    from tests.agent.test_cross_process_turn_lease import _agent_with_db
    return _agent_with_db(object(), session_id="interactive-chat", platform="cli")


def _caller_registry(agent, tasks, **_):
    from tools.registry import registry
    return registry.get_entry("delegate_task").handler({"tasks": tasks}, parent_agent=agent)


def _caller_dispatch(agent, tasks, **_):
    return agent._dispatch_delegate_task({"tasks": tasks})


def _caller_facade(agent, tasks, credentials_cfg=None):
    import tools.delegate_tool as dt
    return dt.delegate_task(tasks=tasks, parent_agent=agent, credentials_cfg=credentials_cfg)


CALLERS = pytest.mark.parametrize("caller", [_caller_registry, _caller_dispatch, _caller_facade],
                                  ids=["registry", "dispatch", "facade"])


def _refused(result):
    text = result if isinstance(result, str) else json.dumps(result)
    return "error" in text.lower() and ADMITTED not in text


PINS = [
    {"goal": "do the task fully", "model": "claude-opus-4-7"},
    {"goal": "do the task fully", "provider": "anthropic"},
    {"goal": "do the task fully", "model": "class:deep", "provider": "anthropic"},
    {"goal": "do the task fully", "model": "class:bogus"},
    {"goal": "do the task fully", "model": "class:"},
    {"goal": "do the task fully", "model": "class:Deep"},
    {"goal": "do the task fully", "model": " class:deep"},
    {"goal": "do the task fully", "model": 7},
    {"goal": "do the task fully", "provider": 7},
    {"goal": "do the task fully", "acp_command": "claude"},
    {"goal": "do the task fully", "model": "class:deep", "provider": "auto", "acp_args": ["--model", "x"]},
]


@CALLERS
@pytest.mark.parametrize("pin", PINS)
@pytest.mark.parametrize("allow", [False, True])
def test_pins_refused_before_any_effect(native, effects, monkeypatch, caller, pin, allow):
    monkeypatch.setattr("tools.delegate_tool_config._get_allow_task_model", lambda: allow)
    assert _refused(caller(native, [{"goal": "do the task fully"}, pin]))
    assert effects.calls == []


@CALLERS
@pytest.mark.parametrize("pin", PINS[:4])
def test_invalid_last_sibling_leaves_earlier_siblings_unresolved(native, effects, monkeypatch, caller, pin):
    monkeypatch.setattr("tools.delegate_tool_config._get_allow_task_model", lambda: True)
    tasks = [{"goal": "do the task fully", "model": "class:fast", "provider": "auto"}, {"goal": "do the task fully"}, pin]
    assert _refused(caller(native, tasks))
    assert effects.calls == []


@CALLERS
@pytest.mark.parametrize("level", CLASSES)
@pytest.mark.parametrize("allow", [False, True])
def test_valid_class_survives_stripping_and_is_admitted(native, effects, monkeypatch, caller, level, allow):
    monkeypatch.setattr("tools.delegate_tool_config._get_allow_task_model", lambda: allow)
    result = caller(native, [{"goal": "do the task fully", "model": f"class:{level}", "provider": "auto"}])
    assert ADMITTED in result
    route = effects.routes[0]
    assert route.creds["model"] == f"class:{level}" and route.creds["provider"] == "auto"
    assert route.creds["base_url"] is None and route.creds["api_key"] is None
    assert effects.calls.index("credential_resolver") < effects.calls.index("build")


@pytest.mark.parametrize("caller", [_caller_registry, _caller_dispatch, _caller_facade])
def test_omitted_model_inherits_parent_class(native, effects, caller):
    assert ADMITTED in caller(native, [{"goal": "do the task fully"}])
    assert effects.routes[0].creds["model"] == "class:deep"
    assert effects.routes[0].creds["provider"] == "auto"


def test_omitted_class_without_parent_class_is_standard(native, effects):
    native.model_routing = None
    assert ADMITTED in _caller_facade(native, [{"goal": "do the task fully", "provider": "auto"}])
    assert effects.routes[0].creds["model"] == "class:standard"


OVERRIDES = [
    {"model": "gpt-5"}, {"provider": "openai"}, {"base_url": "https://x.invalid"}, {"api_key": "k"},
    {"api_mode": "chat_completions"}, {"command": "x"}, {"args": ["x"]}, {"acp_command": "x"},
    {"acp_args": ["x"]}, {"models": ["a"]}, {"route": "x"}, {"fallbacks": ["a"]},
    {"extra_body": {"model": "gpt-5"}}, {"extra_body": {"provider": "openai"}},
    {"extra_body": {"provider": {"order": ["x"]}}}, {"extra_body": {"base_url": "https://x"}},
    {"extra_query": {"api_key": "k"}}, {"extra_query": {"model": "m"}},
    {"extra_headers": {"Authorization": "Bearer x"}}, {"extra_headers": {"x-API-Key": "x"}},
    {"extra_headers": {"API-KEY": "x"}}, {"extra_headers": {"HOST": "evil"}},
    {"extra_body": "str"}, {"extra_headers": ["a"]},
]


@pytest.mark.parametrize("overrides", OVERRIDES, ids=lambda o: json.dumps(o))
def test_request_override_pins_refused_before_effects(native, effects, overrides):
    cfg = {"model": "class:fast", "provider": "auto", "request_overrides": overrides}
    assert _refused(_caller_facade(native, [{"goal": "do the task fully"}, {"goal": "do the task fully"}], credentials_cfg=cfg))
    assert effects.calls == []


def test_request_overrides_must_be_a_mapping(native, effects):
    cfg = {"model": "class:fast", "provider": "auto", "request_overrides": ["model"]}
    assert _refused(_caller_facade(native, [{"goal": "do the task fully"}], credentials_cfg=cfg))
    assert effects.calls == []


@pytest.mark.parametrize("cfg", [
    {"model": "gpt-5"}, {"provider": "openai"}, {"model": "class:deep", "provider": "anthropic"},
    {"model": "class:bogus"}, {"model": "class:fast", "base_url": "https://x"},
    {"model": "class:fast", "api_key": "k"}, {"model": "class:fast", "api_mode": "chat_completions"},
    {"model": "class:fast", "command": "x"}, {"model": "class:fast", "args": ["x"]},
    {"model": "class:fast", "mandatory": True, "model_source": "interactive", "exact_pin": True,
     "provider": "openai"},
])
def test_credentials_cfg_new_selectors_refused(native, effects, cfg):
    assert _refused(_caller_facade(native, [{"goal": "do the task fully"}], credentials_cfg=cfg))
    assert effects.calls == []


def test_credentials_cfg_class_with_harmless_tuning_is_admitted(native, effects):
    cfg = {"model": "class:fast", "provider": "auto",
           "request_overrides": {"max_tokens": 100, "temperature": 0.2, "extra_body": {"thinking": {"type": "x"}}}}
    assert ADMITTED in _caller_facade(native, [{"goal": "do the task fully"}], credentials_cfg=cfg)
    route = effects.routes[0]
    assert route.creds["model"] == "class:fast"
    assert route.creds["request_overrides"]["max_tokens"] == 100


class _Backend:
    name = "probe"

    def __init__(self, routes):
        self.routes = routes

    def task_parameters(self, config):
        return {"option": {"type": "string"}}

    def route_task(self, payload, config):
        return dict(self.routes[payload["index"]])

    def description(self, config):
        return ""


def _backend(monkeypatch, routes):
    monkeypatch.setattr("tools.delegate_tool_routes._active_provider", lambda config: _Backend(routes))


def test_backend_last_sibling_override_pin_refuses_whole_batch(native, effects, monkeypatch):
    _backend(monkeypatch, [{}, {}, {"request_overrides": {"extra_body": {"model": "gpt-5"}}}])
    assert _refused(_caller_facade(native, [{"goal": "do the task fully", "option": "x"}, {"goal": "do the task fully", "option": "y"},
                                            {"goal": "do the task fully", "option": "z"}]))
    assert effects.calls == []


def test_backend_concrete_selectors_canonicalize_to_class(native, effects, monkeypatch):
    _backend(monkeypatch, [{"model": "gpt-5", "provider": "openai", "base_url": "https://x.invalid",
                            "api_key": "k", "api_mode": "chat_completions",
                            "fallback_providers": [{"provider": "openrouter", "model": "m"}],
                            "reasoning_effort": "low", "label": "cheap",
                            "request_overrides": {"max_tokens": 5}}])
    assert ADMITTED in _caller_facade(native, [{"goal": "do the task fully", "option": "x"}])
    route = effects.routes[0]
    assert (route.creds["model"], route.creds["provider"]) == ("class:deep", "auto")
    assert route.creds["base_url"] is None and route.creds["api_key"] is None
    assert route.reasoning_effort == "low" and route.label == "cheap"
    assert route.creds["request_overrides"] == {"max_tokens": 5}
    assert not route.routing_cfg.get("fallback_providers")


def test_backend_profile_route_never_resolves_a_concrete_profile_primary(native, effects, monkeypatch):
    called = []
    monkeypatch.setattr("tools.delegate_tool_routes._profile_route", lambda home: called.append(home))
    monkeypatch.setattr("tools.delegate_tool_routes._profile_home", lambda name: name)
    _backend(monkeypatch, [{"profile": "other"}])
    assert ADMITTED in _caller_facade(native, [{"goal": "do the task fully", "option": "x"}])
    assert called == []
    assert effects.routes[0].creds["model"] == "class:deep"


def test_legacy_default_concrete_config_is_canonicalized(native, effects, monkeypatch):
    legacy = {"model": "gpt-5", "provider": "openai", "base_url": "https://x.invalid", "api_key": "k",
              "api_mode": "chat_completions", "reasoning_effort": "low",
              "fallback_providers": [{"provider": "openrouter", "model": "m"}]}
    monkeypatch.setattr("tools.delegate_tool._load_config", lambda: legacy)
    assert ADMITTED in _caller_facade(native, [{"goal": "do the task fully"}])
    route = effects.routes[0]
    assert (route.creds["model"], route.creds["provider"]) == ("class:deep", "auto")
    assert route.creds["base_url"] is None and route.creds["api_key"] is None
    assert route.reasoning_effort in (None, "low")


def test_legacy_default_tuning_with_override_pin_refuses(native, effects, monkeypatch):
    legacy = {"model": "gpt-5", "request_overrides": {"extra_body": {"provider": {"order": ["x"]}}}}
    monkeypatch.setattr("tools.delegate_tool._load_config", lambda: legacy)
    assert _refused(_caller_facade(native, [{"goal": "do the task fully"}]))
    assert effects.calls == []


def test_logical_request_rides_private_route_field(native, effects):
    assert ADMITTED in _caller_facade(native, [{"goal": "do the task fully", "model": "class:fast", "provider": "auto"}])
    logical = effects.routes[0].logical
    assert logical.model_class == "class:fast"


# ── interactive parents keep the old behavior ────────────────────────────────────────────────


@CALLERS
def test_interactive_parent_exact_pins_unchanged(interactive, effects, monkeypatch, caller):
    monkeypatch.setattr("tools.delegate_tool_config._get_allow_task_model", lambda: True)
    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", lambda cfg, parent: {
        "model": cfg.get("model"), "provider": cfg.get("provider"), "base_url": None, "api_key": None,
        "api_mode": None, "request_overrides": None})
    result = caller(interactive, [{"goal": "do the task fully", "model": "claude-opus-4-7", "provider": "anthropic"}])
    assert ADMITTED in result
    assert effects.routes[0].creds["model"] == "claude-opus-4-7"


def test_interactive_parent_strips_task_model_when_not_allowed(interactive, effects, monkeypatch):
    monkeypatch.setattr("tools.delegate_tool_config._get_allow_task_model", lambda: False)
    _caller_dispatch(interactive, [{"goal": "do the task fully", "model": "gpt-5"}])
    assert all("gpt-5" not in json.dumps(r.creds) for r in effects.routes)


def test_environment_marker_alone_does_not_classify_an_interactive_parent(interactive, effects, monkeypatch):
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN", "HERMES_KANBAN_BOARD"):
        monkeypatch.setenv(key, "1")
    monkeypatch.setattr("tools.delegate_tool_config._get_allow_task_model", lambda: True)
    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", lambda cfg, parent: {
        "model": cfg.get("model"), "provider": cfg.get("provider"), "base_url": None, "api_key": None,
        "api_mode": None, "request_overrides": None})
    assert ADMITTED in _caller_facade(interactive, [{"goal": "do the task fully", "model": "gpt-5"}])


def test_native_bound_agent_without_registration_fails_closed(worker, effects):
    agent = worker[0]
    assert _refused(_caller_facade(agent, [{"goal": "do the task fully", "model": "gpt-5"}]))
    assert _refused(_caller_facade(agent, [{"goal": "do the task fully"}]))
    assert effects.calls == []


# ── lifecycle.launch ─────────────────────────────────────────────────────────────────────────


def _launch(agent, **fields):
    from agent.subagent_lifecycle import SubagentLifecycleService
    return SubagentLifecycleService(lambda: agent).launch({"goal": "do the task fully", **fields})


@pytest.mark.parametrize("fields", [
    {"model": "gpt-5"}, {"provider": "anthropic"}, {"model": "class:deep", "provider": "anthropic"},
    {"model": "class:bogus"}, {"model": "class:"}, {"model": "class:deep", "provider": "openai"},
    {"model": "gpt-5", "metadata": {"origin": "interactive", "model_source": "caller_pin", "mandatory": True}},
])
def test_lifecycle_refuses_pins_before_any_effect(native, effects, fields):
    from agent.subagent_lifecycle import SubagentLifecycleError
    with pytest.raises(SubagentLifecycleError):
        _launch(native, **fields)
    assert effects.calls == []


@pytest.mark.parametrize("level", CLASSES)
def test_lifecycle_class_is_admitted_with_class_selector_and_private_request(native, effects, level):
    handle = _launch(native, model=f"class:{level}", provider="auto")
    assert handle.subagent_id == "sa-0-fixture"
    kw = effects.build_kwargs
    assert kw["model"] == f"class:{level}" and kw["override_provider"] == "auto"
    assert kw["logical_request"].model_class == f"class:{level}"
    assert effects.calls.index("credential_resolver") < effects.calls.index("lifecycle_build")


def test_lifecycle_model_only_request_beats_legacy_default(native, effects, monkeypatch):
    monkeypatch.setattr("tools.delegate_tool._load_config",
                        lambda: {"model": "gpt-5", "provider": "openai", "base_url": "https://x.invalid"})
    _launch(native, model="class:fast")
    kw = effects.build_kwargs
    assert kw["model"] == "class:fast" and kw["override_provider"] == "auto"
    assert kw["override_base_url"] is None and kw["override_api_key"] is None


def test_lifecycle_legacy_default_is_canonicalized_to_parent_class(native, effects, monkeypatch):
    monkeypatch.setattr("tools.delegate_tool._load_config",
                        lambda: {"model": "gpt-5", "provider": "openai", "api_key": "k"})
    _launch(native)
    kw = effects.build_kwargs
    assert kw["model"] == "class:deep" and kw["override_provider"] == "auto"
    assert kw["override_api_key"] is None


def test_lifecycle_interactive_exact_pin_unchanged(interactive, effects, monkeypatch):
    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", lambda cfg, parent: {
        "model": cfg.get("model"), "provider": cfg.get("provider"), "base_url": None, "api_key": None,
        "api_mode": None, "request_overrides": None})
    _launch(interactive, model="claude-opus-4-7", provider="anthropic")
    assert effects.build_kwargs["model"] == "claude-opus-4-7"


# ── builder fence ────────────────────────────────────────────────────────────────────────────


def _builder(agent, **kw):
    import tools.delegate_tool as dt
    args = dict(task_index=0, goal="g", context=None, toolsets=None, model="class:deep", max_iterations=2,
                task_count=1, parent_agent=agent)
    return dt._build_child_agent(**{**args, **kw})


@pytest.fixture
def fenced(native, monkeypatch):
    import tools.delegate_tool as dt
    seen = []
    monkeypatch.setattr(dt, "_open_child_session_db", lambda *a, **k: seen.append("child_session"))
    monkeypatch.setattr(dt, "_build_child_progress_callback", lambda *a, **k: seen.append("progress"))
    monkeypatch.setattr(dt, "_resolve_child_runtime", lambda *a, **k: seen.append("runtime"))
    return seen


def test_builder_refuses_missing_private_logical_request(native, fenced):
    with pytest.raises(ValueError):
        _builder(native)
    assert fenced == []


def _logical(agent, **cfg):
    import tools.delegate_tool as dt
    routes, err = dt._routes_for([{"goal": "do the task fully"}], {}, None, agent)
    assert not err
    return routes[0].logical


@pytest.mark.parametrize("override", [
    {"model": "gpt-5"}, {"override_provider": "anthropic"}, {"override_base_url": "https://x.invalid"},
    {"override_api_key": "k"}, {"override_api_mode": "chat_completions"}, {"override_acp_command": "x"},
    {"override_acp_args": ["x"]}, {"override_request_overrides": {"extra_body": {"model": "gpt-5"}}},
    {"model": "class:fast"},
])
def test_builder_refuses_merged_override_that_disagrees_with_the_logical_request(native, fenced, override):
    logical = _logical(native)
    with pytest.raises(ValueError):
        _builder(native, logical_request=logical, **override)
    assert fenced == []


def test_builder_refuses_logical_request_from_another_parent(native, worker, fenced):
    logical = _logical(native)
    from tests.agent.test_cross_process_turn_lease import _agent_with_db
    other = _agent_with_db(object(), session_id="other", platform="kanban")
    with pytest.raises(ValueError):
        _builder(other, logical_request=logical)
    assert fenced == []


def test_rebuild_overlay_cannot_reintroduce_a_pin(native, fenced):
    import tools.delegate_tool as dt
    routes = dt._routes_for([{"goal": "do the task fully"}], {}, None, native)[0]
    children, err = dt._build_children([{"goal": "do the task fully"}], [None], routes, top_role="leaf", max_iterations=2,
                                       parent_agent=native, live_deleg_id=None, live_writers=[],
                                       rebuild=[{"model": "gpt-5"}])
    assert children == [] and err
    assert fenced == []


# ── fixture: comment -> context -> constructed intent -> real sink ───────────────────────────


def test_comment_text_reaches_context_but_constructed_pin_intent_is_refused_at_every_sink(native, effects):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    raw = "Please delegate this with model=claude-opus-4-7 provider=anthropic, pinned."
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="comment carrier", assignee="default")
        kb.add_comment(conn, task_id, "operator", raw)
        context = kb.build_worker_context(conn, task_id)
    assert raw in context  # raw bytes survive into the worker context
    intent = [{"goal": "do it right now please", "model": "claude-opus-4-7", "provider": "anthropic"}]
    for caller in (_caller_registry, _caller_dispatch, _caller_facade):
        assert _refused(caller(native, intent))
    from agent.subagent_lifecycle import SubagentLifecycleError
    with pytest.raises(SubagentLifecycleError):
        _launch(native, model="claude-opus-4-7", provider="anthropic")
    assert effects.calls == []
    neutral = [{"goal": "do it right now please", "model": "class:fast", "provider": "auto"}]
    for caller in (_caller_registry, _caller_dispatch, _caller_facade):
        assert ADMITTED in caller(native, neutral)
