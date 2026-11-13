"""Already-reserved compute remains able to persist while new work is refused."""
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_reserved_worker_persists_while_runtime_drains(tmp_path):
    from gateway.session_worker import worker_request
    from gateway.session_worker_reservation import reserve_admission_worker
    from tests.gateway.test_worker_admission_reservation import hello
    from hermes_state import SessionDB
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input, RuntimeStoreError
    db = SessionDB(tmp_path / 'state.db')
    process = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE,  # noqa: ASYNC220 -- Popen returns immediately; the test drives the child through its handle
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        db.create_session('owned', 'cli')
        epoch = begin_runtime_epoch(db, instance_id='owner')
        authority = SimpleNamespace(db=db, epoch=epoch, profile_id=str(tmp_path),
            _require_admission_open=lambda: None, logical_owner=lambda sid: sid, sessions={})
        row = admit_session_input(db, epoch=epoch, principal_id='human', session_id='owned', request_id='input', payload={})
        claim_session_input(db, epoch=epoch, session_id='owned')
        scope = reserve_admission_worker(authority, admission_id=row['admission_id'], process=process,
                                         principal_id='human', hello=hello(process))
        actor = SimpleNamespace(subject='human', profile_id=str(tmp_path), capabilities={'worker:adopt'})
        connection = SimpleNamespace(authority=authority, actor=actor)
        identity = {k: v for k, v in scope.items() if k != 'epoch'}
        await worker_request(connection, None, identity, operation='adopt')
        def draining():
            raise RuntimeStoreError('runtime_draining')
        authority._require_admission_open = draining
        await worker_request(connection, None, scope | {
            'sequence': 1, 'operation': 'session.prompt', 'payload': {'system_prompt': 'settled while draining'}}, operation='persist')
        assert db.get_session('owned')['system_prompt'] == 'settled while draining'
        with pytest.raises(RuntimeStoreError, match='runtime_draining'):
            await worker_request(connection, None, identity | {'kind': 'compute'}, operation='register')
    finally:
        process.stdin.close()
        process.wait(timeout=5)
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('purpose', ['worker-adoption', 'interactive'])
@pytest.mark.parametrize('state', ['ready', 'draining'])
async def test_draining_ws_redeems_only_worker_ticket(tmp_path, monkeypatch, purpose, state):
    from gateway.run_api import GatewayRuntimeAPI
    from gateway.runtime_bootstrap import TicketStore
    from hermes_cli import web_server as web
    from hermes_cli import web_server_chat
    tickets = TicketStore('owner', [str(tmp_path)])
    ticket = tickets.mint(profile_id=str(tmp_path), subject='human', purpose=purpose)
    authority = SimpleNamespace(db=SimpleNamespace(db_path=tmp_path / 'state.db'), profile_id=str(tmp_path), runner=None)
    runner = SimpleNamespace(_draining=True, session_runtime_descriptor={'state': state},
                             session_authority=authority, session_ticket_store=tickets)
    seen, sent = [], []
    async def fallback(*args):
        pytest.fail('draining native calls must not enter legacy fallback')
    async def handle(ws, **kwargs):
        await ws.accept(subprotocol='hermes-gateway-v1')
        seen.append((await ws.receive_json(), kwargs['auth_identity']['capabilities']))
    monkeypatch.setattr('tui_gateway.ws.handle_ws', handle)
    monkeypatch.setattr(web, '_DASHBOARD_EMBEDDED_CHAT_ENABLED', True)
    monkeypatch.setattr(web_server_chat, '_ws_request_is_allowed', lambda ws: True)
    incoming = iter([{'type': 'websocket.connect'}, {'type': 'websocket.receive', 'text': json.dumps({'method': 'worker.persist'})}])
    async def receive(): return next(incoming)
    async def send(value): sent.append(value)
    scope = {'type': 'websocket', 'path': '/api/ws', 'scheme': 'ws', 'query_string': b'',
        'client': ('127.0.0.1', 1234), 'server': ('127.0.0.1', 9999),
        'headers': [(b'sec-websocket-protocol', ('hermes-gateway-v1, hermes-gateway-ticket.' + ticket).encode())],
        'subprotocols': ['hermes-gateway-v1', 'hermes-gateway-ticket.' + ticket]}
    await GatewayRuntimeAPI(fallback, runner, web.app)(scope, receive, send)
    if purpose == 'worker-adoption':
        assert seen == [({'method': 'worker.persist'}, frozenset({'worker:adopt'}))]
    else:
        assert not seen
        assert sent[-1]['type'] == 'websocket.close'


@pytest.mark.asyncio
async def test_listener_and_tickets_live_until_settlement(tmp_path, monkeypatch):
    from gateway.run_runtime import drain_gateway_runtime, settle_gateway_runtime
    from gateway.runtime_bootstrap import TicketStore
    tickets = TicketStore('owner', [str(tmp_path)])
    ticket = tickets.mint(profile_id=str(tmp_path), subject='human', purpose='worker-adoption')
    closed = []
    async def stop_api(handle): closed.append(handle)
    async def stop_service(runner): pass
    monkeypatch.setattr('gateway.run_api.stop_gateway_api', stop_api)
    monkeypatch.setattr('gateway.session_hosted_service.stop_hosted_service', stop_service)
    runner = SimpleNamespace(_draining=False, adapters={}, session_runtime_descriptor={'state': 'ready'},
        session_ticket_store=tickets, session_api='listener')
    await drain_gateway_runtime(runner)
    assert not closed
    assert tickets.redeem(ticket, profile_id=str(tmp_path), purpose='worker-adoption')['subject'] == 'human'
    await settle_gateway_runtime(runner)
    assert closed == ['listener']


@pytest.mark.platforms("linux", "macos")
def test_close_tolerates_a_control_frame_that_raced_the_childs_exit():
    """A restart drain's Stop written after the child already exited fails its flush (EPIPE) and
    leaves the frame in stdin's buffer; close() must not re-raise it while flushing on close."""
    from gateway.session_managed_worker import ManagedWorker
    process = subprocess.Popen([sys.executable, '-c', 'pass'], stdin=subprocess.PIPE,  # exits at once
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process.wait(timeout=10)
    worker = ManagedWorker(process)
    with pytest.raises(BrokenPipeError):
        worker.send({'type': 'stop'})  # what _write_controls swallows, leaving the frame buffered
    worker.close()
    assert process.stdin.closed and process.stdout.closed and process.stderr.closed


@pytest.mark.platforms("linux")
def test_finished_turn_keeps_its_answer_when_worker_teardown_raises(tmp_path, monkeypatch):
    """A teardown OSError in execute_managed's finally must never replace the completed reply."""
    import asyncio
    from gateway import session_managed_worker as managed
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionActor, SessionRef, Submission
    from gateway.session_policy import build_policy
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch, claim_session_input
    from tests.gateway.test_managed_worker_finished_exit import CHILD

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('s', source='test')
    runner = SimpleNamespace(_draining=False, _cached_agent_for=lambda route: None,
                             _adapter_for_source=lambda source: None)
    authority = SessionAuthority(runner, profile_id='owned', instance_id='i', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='i'))
    authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='human', chat_id='c'), 'route')
    actor = SessionActor('human', 'owned', frozenset({'session:submit', 'session:control'}), 'cli')
    ref = SessionRef('owned', 's')
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)

    class Child(subprocess.Popen):
        def __init__(self, args, **kw):
            super().__init__([sys.executable, '-c', CHILD, 'nonzero'], **kw)
    monkeypatch.setattr(managed.subprocess, 'Popen', Child)
    real_close = managed.ManagedWorker.close
    closed = []

    def close_then_break(self):
        real_close(self)
        closed.append(self)
        raise BrokenPipeError(32, 'Broken pipe')
    monkeypatch.setattr(managed.ManagedWorker, 'close', close_then_break)
    policy = build_policy({'cwd': str(tmp_path), 'model': 'm'}, {}, profile_terminal=False)

    async def scenario():
        await authority.submit(actor, Submission('turn', ref, {'text': 'go'}, 'queue'))
        row = claim_session_input(db, epoch=authority.epoch, session_id='s')
        authority._stamp_claim(ref, authority.sessions['s'], row)
        async with asyncio.timeout(40):
            return await managed.execute_managed(authority, ref, row, policy)
    with db:
        response = asyncio.run(scenario())
    assert response == 'FINISHED_ANSWER'
    assert closed, 'the worker was never torn down'


@pytest.mark.platforms("linux", "macos")
def test_close_finishes_when_closing_stdout_or_stderr_fails():
    """Failing stdout/stderr closes (no reader or drain thread owns them) must not skip the
    remaining close or raise out of close()."""
    from gateway.session_managed_worker import ManagedWorker
    process = subprocess.Popen([sys.executable, '-c', 'pass'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process.wait(timeout=10)
    real_stdout, real_stderr = process.stdout, process.stderr

    class FailingClose:
        def __init__(self, pipe):
            self.pipe = pipe

        def close(self):
            self.pipe.close()
            raise OSError(9, 'Bad file descriptor')
    process.stdout, process.stderr = FailingClose(real_stdout), FailingClose(real_stderr)
    ManagedWorker(process).close()
    assert real_stdout.closed and real_stderr.closed


@pytest.mark.asyncio
async def test_restart_loss_is_booked_when_worker_teardown_raises(monkeypatch):
    """The restart-lost child's close raising must not skip the uncounted requeue booking."""
    import gateway.session_kanban_restart as r
    booked = []
    monkeypatch.setattr(r, '_restart_stop', lambda cause: True)

    async def book(authority, policy, checkpoint=None):
        booked.append(policy)

    def close():
        raise BrokenPipeError(32, 'Broken pipe')
    monkeypatch.setattr(r, 'book_restart_interruption', book)
    policy = SimpleNamespace(kanban_json='{}')
    cleanup = await r.close_and_book_restart_loss(object(), policy, SimpleNamespace(stop_cause='restart', close=close))
    assert booked == [policy]
    cleanup()  # the caller's finally must not close the same child again
