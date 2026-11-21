"""Cross-revision submitters require an explicit reviewed capsule contract."""
import asyncio

from dataclasses import replace

import pytest

from hermes_cli import kanban_worker_client
from hermes_cli.kanban_capsule_owner import CAPABILITY
from tests.hermes_cli.test_kanban_capsule_owner_binding import (
    BASE_CAPS, OTHER_SHA, _Stop, _board, _discovery, _endpoint,
)


def test_contract_owner_accepts_cross_revision_before_ticket(tmp_path, monkeypatch):
    params, db = _board(tmp_path, monkeypatch)
    calls = []
    endpoint = _endpoint(BASE_CAPS | {CAPABILITY, 'kanban-capsule-contract-v2'}, OTHER_SHA)
    _discovery(monkeypatch, endpoint, calls)
    with pytest.raises(_Stop, match='owner check passed'):
        asyncio.run(kanban_worker_client.run(params, db))
    assert calls == ['ensure', 'ticket']


@pytest.mark.parametrize('caps,sha', [
    (BASE_CAPS | {CAPABILITY, 'kanban-capsule-contract-v3'}, OTHER_SHA),
    (BASE_CAPS | {'kanban-capsule-contract-v2'}, OTHER_SHA),
    (BASE_CAPS | {CAPABILITY, 'kanban-capsule-contract-v2'}, None),
    (BASE_CAPS | {CAPABILITY, 'kanban-capsule-contract-v2'}, 'not-a-sha'),
])
def test_unreviewed_or_unidentified_contract_refuses_before_ticket(tmp_path, monkeypatch, caps, sha):
    from hermes_cli.kanban_capsule_owner import CapsuleOwnerRefused
    params, db = _board(tmp_path, monkeypatch)
    calls = []
    _discovery(monkeypatch, _endpoint(caps, sha), calls)
    with pytest.raises(CapsuleOwnerRefused):
        asyncio.run(kanban_worker_client.run(params, db))
    assert calls == ['ensure']


@pytest.mark.asyncio
async def test_cross_revision_submitter_reaches_real_rpc_and_preserves_failure(tmp_path, monkeypatch):
    """Real client wire/receipt path; discovery/ticket and owner handler are fixtures.

    This composes beyond the before-ticket tracer, but is not full historical
    submitter -> native owner -> private observer capsule execution.
    """
    import json
    from websockets.asyncio.server import serve
    from hermes_cli import gateway_client
    from hermes_cli.gateway_runtime import GatewayDiscovery
    from tests.hermes_cli.test_kanban_capsule_owner_binding import OWN_SHA

    params, db = _board(tmp_path, monkeypatch)
    calls, requests = [], []
    endpoint = _endpoint(BASE_CAPS | {CAPABILITY, 'kanban-capsule-contract-v2'}, OTHER_SHA)
    _discovery(monkeypatch, endpoint, calls)
    assert endpoint.code_sha != OWN_SHA
    # Only the credential mint/discovery are seams; the actual client dials,
    # negotiates its ticket subprotocol and submits the exact native claim.
    monkeypatch.setattr(gateway_client, '_session_ticket', lambda *args, **kw: 'fixture-ticket')

    async def peer(ws):
        assert 'hermes-gateway-ticket.fixture-ticket' in ws.request.headers['Sec-WebSocket-Protocol']
        async for raw in ws:
            request = json.loads(raw)
            requests.append(request)
            assert request['method'] == 'kanban.run'
            result = {'session_id': 'fixture-session', 'receipt': {
                'status': 'unknown', 'admission_id': 'fixture-admission'}}
            await ws.send(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}))

    async with serve(peer, '127.0.0.1', 0, subprotocols=[gateway_client.GATEWAY_WS_PROTOCOL]) as server:
        endpoint = replace(endpoint, api_origin=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}')
        monkeypatch.setattr('hermes_cli.gateway_runtime.ensure_gateway_runtime',
                            lambda *args, **kw: GatewayDiscovery('ready', endpoint))
        with pytest.raises(gateway_client.GatewayClientError, match='Kanban execution unknown; retained by owner'):
            await asyncio.wait_for(kanban_worker_client.run(params, db), 10)
    assert len(requests) == 1  # unknown failure never becomes a reconnect/replay
    assert requests[0]['params'] == {**params, 'db': db}
