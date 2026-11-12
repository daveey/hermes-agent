"""Model provenance survives both local and owner-reserved worker child persistence."""
import json
import os
from functools import partial

import psutil
import pytest

import hermes_state_runtime as rt
from agent.runtime_session_store import RuntimeSessionStore
from gateway.session_worker_child import reserve_child_execution
from hermes_cli.process_identity import worker_birth
from hermes_state import SessionDB
from tests.tools import test_delegate_transcript_readiness as readiness
from tools import delegate_tool as delegate


offline_children = readiness.offline_children


@pytest.mark.parametrize("entry", ["delegate", "lifecycle"])
@pytest.mark.parametrize("worker", [False, True], ids=["desktop", "kanban-worker"])
@pytest.mark.parametrize("case,expected,requested", [
    ("pin", "caller_pin", "pinned-model"),
    ("default", "delegation_default", "default-model"),
    ("inherit", "parent_inherit", "synthetic-model"),
    ("provider-pin", "caller_pin", "default-model"),
])
def test_child_model_source_at_publication_and_first_turn(
    tmp_path, monkeypatch, offline_children, entry, worker, case, expected, requested
):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", source="kanban" if worker else "desktop")
    store = db
    if worker:
        epoch = rt.begin_runtime_epoch(db, instance_id="owner")
        rt.register_worker_execution(db, epoch=epoch, execution_id="worker", session_id="parent",
                                     generation=0, kind="compute", adoption_secret="fixture-secret")
        reserve = partial(reserve_child_execution, epoch=epoch, execution_id="worker",
                          identity={"profile_id": "home", "pid": os.getpid(),
                                    "birth": worker_birth(psutil.Process())},
                          principal="local", limits=(2, 1), refused=None)

        def rpc(method, **params):
            fields = {k: params[k] for k in (
                "epoch", "execution_id", "session_id", "generation", "sequence", "operation", "payload")}
            if fields["operation"] == "child.reserve":
                return rt.apply_worker_receipt(db, **fields, handlers={"child.reserve": reserve})
            return rt.mutate_worker_execution(db, **fields)

        store = RuntimeSessionStore(rpc, dict(epoch=epoch, execution_id="worker", session_id="parent",
                                             generation=0), tmp_path / "outbox")
    published = []

    def progress(event, *args, **payload):
        if event == "subagent.spawn_requested":
            published.append(json.loads(db.get_session(payload["child_session_id"])["model_config"]))

    parent = readiness._parent(store, "parent", progress)
    config = {} if case == "inherit" else {
        "model": "default-model", "provider": "custom", "base_url": parent.base_url,
        "api_key": "synthetic-unused-key"}
    monkeypatch.setattr(delegate, "_load_config", lambda: config)
    task = {"goal": "Inspect synthetic input"}
    if case == "pin":
        task["model"] = "pinned-model"
    if case == "provider-pin":
        task["provider"] = "custom"
    routes, error = delegate._routes_for([task], config, None, parent)
    assert error is None
    children = []
    try:
        if entry == "lifecycle":
            from agent import subagent_lifecycle as lifecycle
            from concurrent.futures import Future
            monkeypatch.setattr(lifecycle._EXECUTOR, "submit", lambda *a: Future())
            if case == "provider-pin":
                monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kw: {
                    "provider": "custom", "model": "default-model", "base_url": parent.base_url,
                    "api_key": parent.api_key, "api_mode": parent.api_mode})
            handle = lifecycle.SubagentLifecycleService(lambda: parent).launch(
                lifecycle.SubagentLaunchRequest(goal=task["goal"], model=task.get("model"),
                                                provider=task.get("provider")))
            record = lifecycle._REGISTRY.records.pop(handle.subagent_id)
            child = record.agent
            children = [(0, task, child)]
        else:
            children, error = delegate._build_children([task], [None], routes, top_role="leaf",
                                                      max_iterations=2, parent_agent=parent,
                                                      live_deleg_id=None, live_writers=[])
            assert error is None
            child = children[0][2]
        assert len(published) == 1
        assert published[0]["model_source"] == expected
        assert published[0]["requested_model"] == requested
        assert published[0]["_delegate_from"] == "parent"
        assert child.model == requested  # recording cannot change routing
        child._cached_system_prompt = "Synthetic prompt"
        child._ensure_db_session()
        persisted = json.loads(db.get_session(child.session_id)["model_config"])
        assert persisted["model_source"] == expected
        assert persisted["requested_model"] == requested
        assert persisted["_delegate_from"] == "parent"
    finally:
        for _, _, child in children:
            child.close()
        if worker:
            store.close()
        db.close()


def _local_child(tmp_path, monkeypatch, task, config, credentials_cfg=None):
    """A real child built through ``_routes_for``/``_build_children`` on a local SessionDB parent."""
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", source="desktop")
    parent = readiness._parent(db, "parent", None)
    monkeypatch.setattr(delegate, "_load_config", lambda: config)
    routes, error = delegate._routes_for([task], config, credentials_cfg, parent)
    assert error is None
    children, error = delegate._build_children([task], [None], routes, top_role="leaf", max_iterations=2,
                                               parent_agent=parent, live_deleg_id=None, live_writers=[])
    assert error is None
    return db, parent, children[0][2]


@pytest.mark.parametrize("pin", [False, True], ids=["no-pin", "task-pin-ignored"])
def test_profile_route_child_is_a_delegation_default_not_parent_inherit(
    tmp_path, monkeypatch, offline_children, pin
):
    """A backend route naming a profile runs the profile's own model (a task pin is ignored), so the row must
    say ``delegation_default`` even though the route's routing_cfg carries only the profile's fallback chain."""
    from agent.delegation_provider import DelegationProvider
    from agent.delegation_registry import _registry
    from tests.tools import test_delegation_profile_route as profile_routes

    launch = tmp_path / "os-home" / ".hermes"
    profile_routes._write_profile(launch, host="launch.invalid", model="launch-model", key="launch-key",
                                  secret="launch-secret", effort="medium")
    profile_routes._write_profile(launch / "profiles" / "coder", host="coder.invalid", model="coder-model",
                                  key="coder-key", secret="coder-secret", effort="low")
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("PROFILE_KEY", "launch-key")

    class Profiles(DelegationProvider):
        @property
        def name(self):
            return "probe-model-source-profile"

        def route_task(self, task, config):
            return {"profile": "coder"}

    provider = Profiles()
    _registry.register(provider)
    try:
        task = {"goal": "Inspect synthetic input"}
        if pin:
            task["model"] = "ignored-pin"
        db, _, child = _local_child(tmp_path, monkeypatch, task, {"backend": provider.name})
        try:
            assert child.model == "coder-model"  # the profile's own model, never the pin
            child._cached_system_prompt = "Synthetic prompt"
            child._ensure_db_session()
            config = json.loads(db.get_session(child.session_id)["model_config"])
            assert config["model_source"] == "delegation_default"
            assert config["requested_model"] == "coder-model"
        finally:
            child.close()
            db.close()
    finally:
        _registry.restore_registration(provider.name, provider, None)


def test_review_route_child_records_delegation_default(tmp_path, monkeypatch, offline_children):
    """/review passes auxiliary.review as ``credentials_cfg``; that route owns the model, so it is a default."""
    parent_url = readiness._parent(None, "parent", None).base_url
    review = {"model": "review-model", "provider": "custom", "base_url": parent_url, "api_key": "synthetic-key"}
    db, _, child = _local_child(tmp_path, monkeypatch, {"goal": "Review synthetic diff"}, {}, credentials_cfg=review)
    try:
        child._cached_system_prompt = "Synthetic prompt"
        child._ensure_db_session()
        config = json.loads(db.get_session(child.session_id)["model_config"])
        assert (config["model_source"], config["requested_model"]) == ("delegation_default", "review-model")
    finally:
        child.close()
        db.close()


def test_compression_continuation_row_of_a_child_keeps_model_provenance(tmp_path, monkeypatch, offline_children):
    from unittest.mock import MagicMock

    config = {"model": "default-model", "provider": "custom", "base_url": "http://127.0.0.1:9/v1",
              "api_key": "synthetic-unused-key"}
    db, _, child = _local_child(tmp_path, monkeypatch, {"goal": "Inspect synthetic input"}, config)
    try:
        child._cached_system_prompt = "Synthetic prompt"
        child._ensure_db_session()
        db.append_message(child.session_id, "user", "question")
        db.append_message(child.session_id, "assistant", "answer")
        messages = db.get_messages_as_conversation(child.session_id)
        compressor = MagicMock()
        compressor.compress.return_value = [{"role": "user", "content": "[CONTEXT COMPACTION] summary"},
                                            {"role": "user", "content": "tail"}]
        compressor.compression_count = 1
        compressor.last_prompt_tokens = compressor.last_completion_tokens = 0
        compressor._last_summary_error = compressor._last_aux_model_failure_model = None
        compressor._last_aux_model_failure_error = None
        compressor._last_compress_aborted = False
        child.context_compressor = compressor
        child._compression_feasibility_checked = True
        child.compression_in_place = False
        old_id = child.session_id
        child._compress_context(messages, "sys", approx_tokens=120_000)
        assert child.session_id != old_id
        continuation = json.loads(db.get_session(child.session_id)["model_config"])
        assert continuation["model_source"] == "delegation_default"
        assert continuation["requested_model"] == "default-model"
    finally:
        child.close()
        db.close()


def test_direct_build_fallback_prefers_caller_pin_over_delegation_default(monkeypatch, offline_children):
    """When ``model_source`` is not passed, the fallback orders caller pin first, as both callers do."""
    from types import SimpleNamespace

    seen = []

    def stop(parent_agent, model_provenance=None):
        seen.append(model_provenance)
        raise ValueError("stop after provenance")

    monkeypatch.setattr(delegate, "_open_child_session_db", stop)
    monkeypatch.setattr(delegate, "_load_config", dict)
    parent = readiness._parent(None, "parent", None)
    with pytest.raises(ValueError, match="stop after provenance"):
        delegate._build_child_agent(
            task_index=0, goal="Inspect", context=None, toolsets=None, model="pinned-model", max_iterations=2,
            task_count=1, parent_agent=parent, routing_cfg={"model": "default-model"})
    assert seen[0]["model_source"] == "caller_pin"
    assert SimpleNamespace(**seen[0]).requested_model == "pinned-model"
