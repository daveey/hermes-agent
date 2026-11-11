"""A supervisor definition refusal must not hide a returning private control owner."""
import asyncio
import time
from unittest.mock import Mock

import pytest

from hermes_cli import gateway_runtime as runtime
from hermes_cli import gateway_runtime_service as service
from hermes_cli import gateway_runtime_start as start


@pytest.mark.platforms("posix")
def test_restart_gap_reverifies_returning_control_owner(tmp_path, monkeypatch):
    from gateway.control_socket import GatewayControlServer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(runtime, "_update_fenced", lambda homes: False)
    monkeypatch.setattr(runtime, "_starting_multiplexer_root", lambda home: None)
    monkeypatch.setattr(runtime, "_served_by_multiplexer", lambda *args, **kwargs: None)
    from hermes_cli import gateway_runtime_multiplex as multiplex
    monkeypatch.setattr(multiplex, "multiplexer_serves_home", lambda home: None)
    monkeypatch.setattr(multiplex, "implied_host_root", lambda home: None)
    spawn = Mock(side_effect=AssertionError("must not spawn an unverified owner"))
    launch = Mock(side_effect=AssertionError("must not execute an unverified service"))
    monkeypatch.setattr(start, "spawn_unmanaged_gateway", spawn)
    monkeypatch.setattr(service, "start_existing_gateway_service", launch)

    async def probe():
        payload = {"runtime_protocol": 1, "state": "ready", "instance_id": "old",
                   "authority_epoch": 1, "api_origin": "http://127.0.0.1:12345",
                   "supervisor": "external", "capabilities": ["session-authority-v1"],
                   "served_profiles": [{"profile_id": str(home), "home": str(home)}]}
        server = GatewayControlServer(home, verb_handlers={"identify": lambda: payload})
        assert await server.start()
        initial = await asyncio.to_thread(runtime.ensure_gateway_runtime, home, timeout=5)
        assert initial.endpoint is not None
        assert initial.endpoint.instance_id == "old"
        await server.stop()
        refused = asyncio.Event()
        loop = asyncio.get_running_loop()

        def unsupported(*args, **kwargs):
            loop.call_soon_threadsafe(refused.set)
            raise service.RuntimeStartError("service_identity_unverified")

        monkeypatch.setattr(service, "discover_existing_gateway_service", unsupported)
        before = time.monotonic()
        recovering = asyncio.create_task(asyncio.to_thread(runtime.ensure_gateway_runtime, home, timeout=5))
        try:
            await asyncio.wait_for(refused.wait(), 3)
            payload.update(instance_id="replacement", authority_epoch=2)
            assert await server.start()
            result = await recovering
            assert result.state == "ready", result
            assert result.endpoint is not None
            assert result.endpoint.instance_id == "replacement"
            assert result.endpoint.authority_epoch == 2
            assert time.monotonic() - before < 30
            spawn.assert_not_called()
            launch.assert_not_called()
        finally:
            await server.stop()
            await recovering

    asyncio.run(probe())


def test_permanent_service_refusal_preserves_reason_and_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_update_fenced", lambda homes: False)
    monkeypatch.setattr(runtime, "discover_gateway_endpoint", lambda *args, **kwargs: runtime.GatewayDiscovery("absent"))
    monkeypatch.setattr(runtime, "_starting_multiplexer_root", lambda home: None)
    from hermes_cli import gateway_runtime_multiplex as multiplex
    monkeypatch.setattr(multiplex, "multiplexer_serves_home", lambda home: None)
    monkeypatch.setattr(multiplex, "implied_host_root", lambda home: None)
    refused = Mock(side_effect=service.RuntimeStartError("service_identity_unverified"))
    monkeypatch.setattr(service, "discover_existing_gateway_service", refused)
    spawn = Mock(side_effect=AssertionError("unverified service is not absence"))
    monkeypatch.setattr(start, "spawn_unmanaged_gateway", spawn)
    before = time.monotonic()
    result = runtime.ensure_gateway_runtime(tmp_path, timeout=2)
    assert result.state == "inaccessible"
    assert result.reason_code == "service_identity_unverified"
    assert time.monotonic() - before < 5
    refused.assert_called_once()
    spawn.assert_not_called()


def test_observation_caps_long_caller_budget_at_thirty_seconds(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda duration: now.__setitem__(0, now[0] + duration))
    monkeypatch.setattr(runtime, "_update_fenced", lambda homes: False)
    monkeypatch.setattr(runtime, "discover_gateway_endpoint", lambda *args, **kwargs: runtime.GatewayDiscovery("absent"))
    result = runtime._await_unverified_service_owner(tmp_path, 160.0)
    assert result.reason_code == "service_identity_unverified"
    assert now[0] == pytest.approx(130.0)


@pytest.mark.parametrize("verdict", [
    runtime.GatewayDiscovery("inaccessible", reason_code="profile_mismatch"),
    runtime.GatewayDiscovery("incompatible", reason_code="runtime_protocol"),
    runtime.GatewayDiscovery("draining", reason_code="update_paused"),
])
def test_returning_owner_refusal_is_not_readiness(tmp_path, monkeypatch, verdict):
    monkeypatch.setattr(runtime, "_update_fenced", lambda homes: False)
    monkeypatch.setattr(runtime, "discover_gateway_endpoint", lambda *args, **kwargs: verdict)
    assert runtime._await_unverified_service_owner(tmp_path, time.monotonic() + 5) == verdict


def test_update_fence_arriving_during_observation_blocks_attach(tmp_path, monkeypatch):
    fence = Mock(side_effect=[False, True])
    monkeypatch.setattr(runtime, "_update_fenced", fence)
    discover = Mock(return_value=runtime.GatewayDiscovery("absent"))
    monkeypatch.setattr(runtime, "discover_gateway_endpoint", discover)
    monkeypatch.setattr(time, "sleep", lambda duration: None)
    verdict = runtime.GatewayDiscovery("draining", reason_code="update_paused")
    monkeypatch.setattr(runtime, "_fenced_verdict", lambda *args: verdict)
    assert runtime._await_unverified_service_owner(tmp_path, time.monotonic() + 5) == verdict
    discover.assert_called_once()



@pytest.mark.parametrize("reason", ["service_unavailable", "service_failed", None])
def test_other_start_refusals_are_not_observed_or_delayed(tmp_path, monkeypatch, reason):
    discover = Mock(side_effect=AssertionError("only an unverified definition may await its owner"))
    monkeypatch.setattr(runtime, "discover_gateway_endpoint", discover)
    monkeypatch.setattr(time, "sleep", Mock(side_effect=AssertionError("must not wait")))
    refusal = runtime.GatewayDiscovery("inaccessible", reason_code=reason)
    assert runtime._recover_start_refusal(tmp_path, time.monotonic() + 5, refusal) is refusal
    discover.assert_not_called()


def test_observation_keeps_waiting_through_starting_until_owner_verdict(tmp_path, monkeypatch):
    ready = runtime.GatewayDiscovery("ready")
    seq = iter([runtime.GatewayDiscovery("absent"), runtime.GatewayDiscovery("starting"), ready])
    monkeypatch.setattr(runtime, "_update_fenced", lambda homes: False)
    monkeypatch.setattr(runtime, "discover_gateway_endpoint", lambda *args, **kwargs: next(seq))
    monkeypatch.setattr(time, "sleep", lambda duration: None)
    assert runtime._await_unverified_service_owner(tmp_path, time.monotonic() + 5) is ready


def test_an_owner_that_never_returns_keeps_the_start_refusal_and_its_detail(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda duration: now.__setitem__(0, now[0] + duration))
    monkeypatch.setattr(runtime, "_update_fenced", lambda homes: False)
    monkeypatch.setattr(runtime, "discover_gateway_endpoint", lambda *args, **kwargs: runtime.GatewayDiscovery("absent"))
    refusal = runtime.GatewayDiscovery("inaccessible", reason_code="service_identity_unverified",
                                       detail="unit runs as DynamicUser=yes")
    assert runtime._await_unverified_service_owner(tmp_path, 105.0, refusal) == refusal
