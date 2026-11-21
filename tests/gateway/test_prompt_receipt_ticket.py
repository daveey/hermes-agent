"""Receipt pollers must not inherit the native interactive operator grant."""
from types import SimpleNamespace
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio

from gateway.session_contract import SessionActor, Submission
from gateway.session_controls import AuthorityConnection
from hermes_state_runtime import RuntimeStoreError, get_session_admission


@pytest_asyncio.fixture
async def receipt_owner(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority

    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False)
    authority = await initialize_session_authority(runner, profile_id=str(Path(store._db.db_path).parent), instance_id='fixture')
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    try:
        yield authority
    finally:
        store._db.close()


async def _queued(authority, subject, request, principal=None, source='gui'):
    from gateway.session_local import create_local_session

    actor = SessionActor(subject, authority.profile_id,
                         frozenset({'session:create', 'session:submit'}), request)
    ref = create_local_session(authority, actor, {'request_id': request, 'source': source,
        'cwd': authority.profile_id, 'model': 'fixture', 'toolsets': []})
    if principal is not None:
        actor = replace(actor, subject=principal, capabilities=actor.capabilities | {'session:operator'})
    receipt = await authority.submit(actor, Submission(request, ref, {'text': 'fixture'}, 'queue'))
    return ref, receipt


async def _redeem(authority, monkeypatch, purpose):
    from gateway import run_api
    from gateway.owner_identity import host_owner_subject
    from gateway.runtime_bootstrap import TicketStore
    from hermes_cli import web_server as web
    from tests.gateway.test_session_socket_preload import _attach_scope

    runner = authority.runner
    runner.session_runtime_descriptor = {'state': 'ready'}
    runner.session_ticket_store = TicketStore(authority.instance_id, frozenset({authority.profile_id}))
    ticket = runner.session_ticket_store.mint(profile_id=authority.profile_id,
        subject=host_owner_subject(), purpose=purpose)
    connections = []

    async def handler(ws, *, auth_identity, operator, subprotocol):
        connections.append(AuthorityConnection(authority, object(), auth_identity, operator=operator))
        await ws.close()

    async def load_handler():
        return handler

    async def fallback(*args):
        pytest.fail('native ticket fell through to browser authentication')

    async def receive():
        return {'type': 'websocket.connect'}

    async def send(message):
        pass

    monkeypatch.setattr(run_api, 'session_socket_handler', load_handler)
    monkeypatch.setattr(web, '_DASHBOARD_EMBEDDED_CHAT_ENABLED', True)
    from hermes_cli import web_server_chat
    monkeypatch.setattr(web_server_chat, '_ws_request_is_allowed', lambda ws: True)
    await run_api.GatewayRuntimeAPI(fallback, runner, web.app)(_attach_scope(ticket), receive, send)
    assert len(connections) == 1
    return connections[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('foreign', ['owner', 'principal', 'both'])
async def test_receipt_ticket_cannot_read_foreign_local_owner(receipt_owner, monkeypatch, foreign):
    from gateway.owner_identity import host_owner_subject

    authority = receipt_owner
    subject = host_owner_subject()
    ref, receipt = await _queued(authority,
        subject if foreign == 'principal' else 'foreign-owner', 'foreign',
        subject if foreign == 'owner' else 'foreign-principal')
    connection = await _redeem(authority, monkeypatch, 'prompt-receipt')
    try:
        reply = await connection.dispatch({'id': 1, 'method': 'prompt.receipt',
            'params': {'session_id': ref.session_id, 'admission_id': receipt.admission_id}})
        assert reply.get('error', {}).get('message') == 'permission_denied', reply
        if foreign == 'principal':
            # Receipt-only reads must not reuse the interactive control bypass even internally.
            elevated = replace(connection.actor, capabilities=connection.actor.capabilities | {'session:control'})
            with pytest.raises(RuntimeStoreError, match='permission_denied'):
                await authority.receipt(elevated, ref, receipt.admission_id, receipt_only=True)
    finally:
        await connection.close()


def test_receipt_ticket_binding_and_single_use():
    from gateway.runtime_bootstrap import TicketStore

    store = TicketStore('fixture', frozenset({'one', 'two'}))
    with pytest.raises(PermissionError, match='bootstrap binding rejected'):
        store.mint(profile_id='one', subject='uid:1000', purpose='prompt-receipt', scope='host')
    ticket = store.mint(profile_id='one', subject='uid:1000', purpose='prompt-receipt')
    for profile, purpose in [('two', 'prompt-receipt'), ('one', 'interactive'), ('one', 'worker-adoption')]:
        with pytest.raises(PermissionError, match='bootstrap binding rejected'):
            store.redeem(ticket, profile_id=profile, purpose=purpose)
    grant = store.redeem(ticket, profile_id='one', purpose='prompt-receipt')
    assert grant['capabilities'] == frozenset({'session:receipt'})
    with pytest.raises(PermissionError, match='invalid bootstrap ticket'):
        store.redeem(ticket, profile_id='one', purpose='prompt-receipt')


@pytest.mark.asyncio
async def test_receipt_ticket_reads_own_admission_but_no_other_rpc(receipt_owner, monkeypatch):
    from gateway.owner_identity import host_owner_subject

    authority = receipt_owner
    ref, receipt = await _queued(authority, host_owner_subject(), 'own')
    connection = await _redeem(authority, monkeypatch, 'prompt-receipt')
    params = {'session_id': ref.session_id, 'admission_id': receipt.admission_id}
    try:
        assert connection.actor.capabilities == frozenset({'session:receipt'})
        reply = await connection.dispatch({'id': 1, 'method': 'prompt.receipt', 'params': params})
        assert reply['result']['admission_id'] == receipt.admission_id
        assert reply['result']['status'] == 'queued'
        before = get_session_admission(authority.db, admission_id=receipt.admission_id)
        # Enumerate the whole core surface plus group and plugin routes, even handlers with no session checks.
        methods = set(connection.handlers()) | {'profiles.list', 'group.create', 'fixture.plugin'}
        for method in sorted(methods - {'prompt.receipt'}):
            denied = await connection.dispatch({'id': 2, 'method': method, 'params': params})
            assert denied['error']['message'] == 'permission_denied', (method, denied)
        # Internal receipt-then-write callers must retain their submit/control gate too.
        with pytest.raises(RuntimeStoreError, match='permission_denied'):
            await authority.cancel_queued(connection.actor, ref, receipt.admission_id)
        assert get_session_admission(authority.db, admission_id=receipt.admission_id) == before
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['gui', 'cli'])
async def test_interactive_owner_keeps_receipts_and_controls(receipt_owner, monkeypatch, source):
    from gateway.owner_identity import host_owner_subject

    authority = receipt_owner
    ref, receipt = await _queued(authority, host_owner_subject(), source, source=source)
    connection = await _redeem(authority, monkeypatch, 'interactive')
    try:
        assert {'session:operator', 'session:control', 'session:submit'} <= connection.actor.capabilities
        params = {'session_id': ref.session_id, 'admission_id': receipt.admission_id}
        result = await connection.dispatch({'id': 1, 'method': 'prompt.receipt', 'params': params})
        assert result['result']['status'] == 'queued'
        cancelled = await connection.dispatch({'id': 2, 'method': 'prompt.cancel', 'params': params})
        assert cancelled['result']['status'] == 'terminal'
    finally:
        await connection.close()
