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
