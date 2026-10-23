"""Cron classes are allocated at the durable per-fire agent boundary."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.agent import test_model_routing_agent
from tests.tui_gateway import test_class_routing

routing_home = test_model_routing_agent.routing_home
streaming_route = test_model_routing_agent.streaming_route
native_transport = test_class_routing.native_transport


def test_cron_setup_and_native_wire(routing_home, streaming_route, native_transport):
    from cron.scheduler import _resolve_job_runtime, _construct_cron_agent, _CronAgentSetup
    from run_agent import AIAgent
    jc = SimpleNamespace(model="class:deep", cron_default_provider="auto", cfg=routing_home)
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=AssertionError("premature allocation")):
        runtime, model = _resolve_job_runtime({"id": "job"}, "job", jc)
    setup = _CronAgentSetup(model=model, runtime=runtime, max_iterations=2,
                           fallback_model=[{"provider": "openrouter", "model": "paid"}])
    with patch("model_tools.get_tool_definitions", return_value=[]):
        agent = _construct_cron_agent(AIAgent, {"id": "job"}, routing_home, setup,
                                      workdir=None, session_id="cron_job_fire", session_db=None)
        assert agent.model_routing["requested_class"] == "deep"
        assert agent._fallback_chain == []
        assert agent.run_conversation("Say ok")["final_response"] == "ok"
        resumed = _construct_cron_agent(AIAgent, {"id": "job"}, routing_home, setup,
                                        workdir=None, session_id="cron_job_fire", session_db=None)
        assert resumed.model_routing == agent.model_routing
        agent.release_clients()
        resumed.release_clients()
    assert native_transport
    assert all(body["model"] == "anthropic-deep" and url.startswith("https://api.anthropic.com/")
               and headers.get("authorization", "").startswith("Bearer sk-ant-oat")
               for url, headers, body in native_transport)


@pytest.mark.parametrize("cfg", [{"model": {"default": "class:deep"}},
                                 {"cron": {"model": "class:deep"}}])
def test_preflight_does_not_resolve_default_provider_for_class(cfg):
    from cron.scheduler_preflight import _preflight_check_provider_key
    from hermes_cli.auth import AuthError
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=AuthError("missing paid key")) as resolve:
        assert _preflight_check_provider_key({"id": "job"}, cfg) is None
    resolve.assert_not_called()


@pytest.mark.parametrize("job", [{"provider": "anthropic"}, {"base_url": "https://paid.invalid"}])
def test_cron_class_rejects_incompatible_pins(job):
    from cron.scheduler import _resolve_job_runtime
    from agent.model_routing import ModelRoutingError
    jc = SimpleNamespace(model="class:deep", cron_default_provider="auto", cfg={})
    with pytest.raises(ModelRoutingError):
        _resolve_job_runtime(job, "job", jc)


@pytest.mark.parametrize("empty", [False, True])
def test_full_cron_fire(routing_home, streaming_route, native_transport, tmp_path, monkeypatch, empty):
    from cron.scheduler import run_job
    from hermes_cli.config import atomic_config_write
    routing_home["cron"] = {"model": "class:deep", "model_provider": "auto"}
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    if empty:
        monkeypatch.setattr("agent.credential_pool.CredentialPool.has_available", lambda *a, **kw: False)
    with patch("model_tools.get_tool_definitions", return_value=[]), \
            patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
            patch("cron.scheduler._init_cron_mcp_tools"), \
            patch("hermes_cli.env_loader.load_hermes_dotenv"), \
            patch("hermes_cli.env_loader.reset_secret_source_cache"):
        success, _, final, error = run_job({"id": "class-fire", "name": "class fire", "prompt": "Say ok"})
    if empty:
        assert not success
        assert "ModelRoutingError" in error
        assert not native_transport
    else:
        assert success, error
        assert final == "ok"
        assert native_transport
        assert all(body["model"] == "anthropic-deep" for _, _, body in native_transport)
