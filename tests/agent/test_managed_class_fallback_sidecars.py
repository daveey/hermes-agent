"""A managed worker whose class route fails over must keep its turn and its receipt journal.

Live failure (run 10210): a class:standard route answered 402 (out of credits), ``try_class_fallback``
mirrored ``model_routing`` onto the session row through the worker's ``RuntimeSessionStore``, the owner
refused ``session.sidecars`` with ``invalid_params``, the refusal latched in the journal and ``close()``
re-raised it, so the worker exited 1 instead of answering on the fallback route.
"""
import json
from copy import deepcopy
from unittest.mock import patch

import anthropic
import httpx
import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError
from hermes_cli.config import atomic_config_write
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch, mutate_worker_execution, register_worker_execution
from tests.agent import test_model_routing_agent
from tests.agent.test_class_failure_wire import completion
from tests.agent.test_model_routing_agent import build_agent

routing_home = test_model_routing_agent.routing_home
streaming_route = test_model_routing_agent.streaming_route


@pytest.fixture
def worker_store(tmp_path):
    db = SessionDB(tmp_path / "owner-state.db")
    db.create_session("managed-class", "cli", model="class:deep")
    epoch = begin_runtime_epoch(db, instance_id="fixture")
    scope = dict(epoch=epoch, execution_id="worker", session_id="managed-class", generation=0)
    register_worker_execution(db, **scope, kind="compute", adoption_secret="test-secret")
    from tests.worker_store_rpc import worker_store_rpc
    store = RuntimeSessionStore(lambda method, **p: worker_store_rpc(db, method, **p), scope, tmp_path / "outbox")
    try:
        yield db, store
    finally:
        try:
            store.close()
        finally:
            db.close()


@pytest.mark.parametrize("mirror_fails", [False, True])
def test_class_route_quota_failure_fails_over_and_worker_persists_model_routing(routing_home, streaming_route, tmp_path,
                                                                     worker_store, mirror_fails):
    """``mirror_fails``: the best-effort row mirror of the failover raises; the turn still answers."""
    db, store = worker_store
    second = deepcopy(routing_home["model_routing"]["routes"]["deep"][0])
    second["model"] = "z-anthropic-deep"
    routing_home["model_routing"]["routes"]["deep"].append(second)
    routing_home["agent"] = {"max_iterations": 4}
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    sent = []

    def respond(request):
        body = json.loads(request.content)
        sent.append(body["model"])
        if body["model"] == "anthropic-deep":
            # 429 quota, not the live 402: this fixture's two routes share ONE pooled credential, and a
            # 402 marks that credential exhausted for both, so no class route would remain. The live 402
            # came from a different provider; the persist path after try_class_fallback is the same.
            return httpx.Response(429, json={"type": "error", "error": {
                "type": "rate_limit_error",
                "message": "You have run out of credits (personal-team-blocked:spending-limit)"}})
        return completion(body["model"])

    sdk_class = anthropic.Anthropic

    def sdk(**kwargs):
        kwargs["http_client"] = httpx.Client(transport=httpx.MockTransport(respond))
        kwargs["max_retries"] = 0
        return sdk_class(**kwargs)

    with patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("agent.title_generator._auto_title_enabled", return_value=False), \
         patch("agent.turn_recovery.interruptible_backoff_sleep", return_value=None), \
         patch("anthropic.Anthropic", side_effect=sdk):
        agent = build_agent(session_id="managed-class", session_db=store, max_iterations=4)
        if mirror_fails:
            real_patch = store.patch_session_model_config

            def refusing_patch(session_id, patch):
                if "model_routing" in patch:
                    raise WorkerPersistenceError("invalid_params")
                return real_patch(session_id, patch)
            store.patch_session_model_config = refusing_patch
        try:
            result = agent.run_conversation("Say ok")
        finally:
            agent.release_clients()
    assert result["final_response"] == "ok", result
    assert sent[0] == "anthropic-deep" and sent[-1] == "z-anthropic-deep", sent
    assert agent.model == "z-anthropic-deep"
    # Nothing is latched: the close-time flush succeeds.
    assert store.failure is None
    assert store.flush_token_counts() is True
    if mirror_fails:
        return
    # The failover reached the owner's row.
    routing = db.get_session_model_config_value("managed-class", "model_routing")
    assert routing and routing["requested_class"] == "deep", routing
    assert db.get_session("managed-class")["model"] == "z-anthropic-deep"


def test_unregistered_sidecar_key_fails_one_call_without_poisoning_the_journal(worker_store):
    db, store = worker_store
    with pytest.raises(WorkerPersistenceError, match="invalid_params"):
        store.patch_session_model_config("managed-class", {"_not_a_registered_sidecar": 1})
    assert store.failure is None and not store.journal["pending"]
    store.patch_session_model_config("managed-class", {"model_routing": {"requested_class": "deep"}})
    assert db.get_session_model_config_value("managed-class", "model_routing") == {"requested_class": "deep"}
    assert store.flush_token_counts() is True

