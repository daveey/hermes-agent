"""A session write rides out the outgoing half of a gateway restart: a plain ``draining`` owner.

Hub, Nov 24 05:23:45Z: a Desktop archive sent 10 s into a planned gateway restart (systemd stop at
05:23:35, 30 s drain, new gateway READY about a minute later) came back at once as a 503
``owner_unavailable: draining: Exclusive maintenance refused: Gateway runtime already owns profile
... Drain and stop the gateway, then retry``. Discovery waited out ``starting`` but treated the
drain before it as final. A plain drain (no reason, or its idle exit) is now waited out on the
same budget; an update's ``update_paused`` fence stays final (its own tests are unchanged).
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from hermes_cli import session_owner_discovery as sod
from hermes_cli.gateway_runtime import GatewayDiscovery
from tests.hermes_cli.test_session_owner_discovery_retry import _Clock, _reads, _standalone_request
from tests.hermes_cli.test_session_owner_discovery_starting import _fast_poll, _held

DRAINING = GatewayDiscovery('draining')
STARTING = GatewayDiscovery('starting')
READY = SimpleNamespace(state='ready', endpoint='owner-endpoint', reason_code=None, detail=None)


@pytest.mark.asyncio
async def test_a_restart_is_waited_out_from_drain_through_start(tmp_path, monkeypatch):
    clock = _Clock()
    start = clock.now
    seen = _reads(monkeypatch, [DRAINING] * 10 + [STARTING] * 10 + [READY], clock, cost=lambda _t: 0.01)
    assert await sod.discover_session_owner(tmp_path, clock=clock, sleep=clock.sleep) is READY
    assert len(seen) == 21
    assert clock.now - start < sod.OWNER_STARTING_BUDGET_S


@pytest.mark.asyncio
async def test_a_drain_that_ends_in_absent_takes_the_offline_path(tmp_path, monkeypatch):
    clock = _Clock()
    absent = GatewayDiscovery('absent')
    seen = _reads(monkeypatch, [DRAINING, DRAINING, absent, READY], clock, cost=lambda _t: 0.01)
    assert await sod.discover_session_owner(tmp_path, clock=clock, sleep=clock.sleep) is absent
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_an_idle_exit_drain_is_waited_out_too(tmp_path, monkeypatch):
    clock = _Clock()
    idle = GatewayDiscovery('draining', reason_code='idle_exit')
    seen = _reads(monkeypatch, [idle, idle, READY], clock, cost=lambda _t: 0.01)
    assert await sod.discover_session_owner(tmp_path, clock=clock, sleep=clock.sleep) is READY
    assert len(seen) == 3


def test_only_a_plain_drain_without_an_endpoint_is_draining():
    assert sod.is_draining(DRAINING) and sod.is_draining(GatewayDiscovery('draining', reason_code='idle_exit'))
    assert not sod.is_draining(GatewayDiscovery('draining', reason_code='update_paused'))
    assert not sod.is_draining(GatewayDiscovery('draining', reason_code='replaced'))
    assert not sod.is_draining(SimpleNamespace(state='draining', endpoint='owner', reason_code=None))
    assert not sod.is_draining(STARTING) and not sod.is_draining(GatewayDiscovery('absent'))


def test_the_drain_detail_asks_for_a_retry_not_a_stop():
    advice = 'Exclusive maintenance refused: held. Drain and stop the gateway, then retry.'
    assert sod.owner_unavailable_detail(DRAINING, advice) == sod.DRAINING_DETAIL
    assert sod.DRAINING_DETAIL.startswith('owner_unavailable: draining: ')
    assert 'Drain' not in sod.DRAINING_DETAIL and 'stop' not in sod.DRAINING_DETAIL


@pytest.mark.asyncio
async def test_a_gateway_still_draining_past_the_budget_answers_retry_not_drain(tmp_path, monkeypatch):
    """The draining gateway holds the lock: the offline write loses (never bypassed), nothing is
    forwarded, and the 503 asks for a retry instead of telling the user to stop the gateway."""
    from hermes_cli import web_server_sessions

    _fast_poll(monkeypatch)
    monkeypatch.setattr(sod, 'OWNER_STARTING_BUDGET_S', 0.3)
    request = _standalone_request(monkeypatch, tmp_path)
    seen = _reads(monkeypatch, [DRAINING])
    attempts, connects = [], []
    monkeypatch.setattr('hermes_cli.gateway_client.connect_gateway', lambda **kw: connects.append(kw))
    with _held(attempts) as held:
        monkeypatch.setattr('gateway.runtime_ownership.exclusive_maintenance', held)
        with pytest.raises(HTTPException) as exc:
            await web_server_sessions._mutate_session_request(
                request, None, 's1', request_id='req-1', expected_revision=0,
                operation='sidebar', payload={'archived': True})
    assert exc.value.status_code == 503
    assert exc.value.detail == sod.DRAINING_DETAIL
    assert len(seen) > sod.OWNER_DISCOVERY_ATTEMPTS and len(attempts) == 1 and connects == []
