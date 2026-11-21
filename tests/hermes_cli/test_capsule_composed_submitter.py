"""Cross-revision wire -> owner placement -> native child -> observation boundary.

Discovery/ticket mint, the owner RPC handler and worker.adopt reply are bounded
fixture peers. Owner disk identity is real; its installed ledger is a unit seam.
The child bootstrap, managed main/execute, claim binding and inherited observation
are unmodified. Checkpoint stops execution before tools/model. This is not private
Ask Eve observer, historical full-core or successful model completion evidence.
"""
import asyncio
import json
import os
import selectors
import socket
import subprocess
import threading
from dataclasses import replace

import pytest

from tests.hermes_cli import test_capsule_native_lifecycle as lifecycle
from tests.hermes_cli import test_kanban_capsule_owner_binding as binding
from tests.gateway import test_session_kanban_rendezvous as rendezvous

core = lifecycle.core
python = lifecycle.python
lifecycle_core = lifecycle.lifecycle_core
pytestmark = lifecycle.pytestmark


class WorkspaceAcceptor(rendezvous.Acceptor):
    def _one(self, conn):
        """The documented host protocol with a real workspace in the start frame."""
        conn.settimeout(5)
        hello = json.loads(conn.makefile('rb').readline())
        self.hellos.append(hello)
        conn.sendall(lifecycle.encode({'v': 1, 'ok': True, **self.identity(),
                                      'dispatch_owner': self.reply_owner}))
        if hello.get('op') != 'attach':
            return
        msg, fds, _flags, _addr = socket.recv_fds(conn, 65536, 1)
        payload = json.loads(msg)
        self.payloads.append(payload)
        assert len(fds) == 1
        channel = socket.socket(fileno=fds[0])
        self.fds.append(channel)
        channel.sendall(lifecycle.encode({'v': 1, 'op': 'start',
            **{key: self.context[key] for key in ('task_id', 'run_id', 'claim_lock', 'workspace')}}))
        conn.sendall(lifecycle.encode({'v': 1, 'ok': True, 'received': True}))


def test_cross_revision_submitter_native_child_observation(tmp_path, lifecycle_core, python, monkeypatch):
    from websockets.sync.server import serve
    from tests.gateway.test_session_kanban_capsule_guard import _policy
    from tests.gateway.test_managed_turn_contract import assignment
    from gateway import capsule_contract as contract
    from gateway.session_kanban_scope import placing_spawn
    from hermes_cli import gateway_client, git_fingerprint, kanban_worker_client
    from hermes_cli.gateway_runtime import GatewayDiscovery

    home = tmp_path / 'profile'
    home.mkdir()
    policy = _policy(home, monkeypatch)
    context = json.loads(policy.kanban_json)
    params = {key: context[key] for key in ('board', 'task_id', 'run_id', 'claim_lock')}
    frame, _ = assignment(home, monkeypatch)
    frame['policy'].update(source='kanban', kanban_json=policy.kanban_json)
    frame['scope'].update(profile_id=str(home), epoch=1, execution_id='capsule-fixture')
    frame['home'] = str(home)
    sha = lifecycle.helpers._git(lifecycle_core, 'rev-parse', 'HEAD').strip()
    assert sha != binding.OWN_SHA
    monkeypatch.setattr(contract, 'CORE_ROOT', lifecycle_core)
    git_fingerprint.record_process_boot_fingerprint(lifecycle_core)
    git_fingerprint.record_capsule_boot_identity(lifecycle_core)
    monkeypatch.setattr('hermes_cli.code_attestation.integrity_observation', lambda: {
        'core_root': str(lifecycle_core), 'boot_sha': sha, 'route': 'managed-stub', 'unknowns': ()})
    monkeypatch.setattr('gateway.session_kanban_scope.resolve_run_scope', lambda policy: None)
    observer = lifecycle.stage_observer(lifecycle.trusted_base(tmp_path))
    from tests.agent.test_managed_worker_observer_load import root_digest
    acceptor = WorkspaceAcceptor(context, registration=rendezvous.registration_for(context['db'],
        observer_root=str(observer), observer_digest=root_digest(observer)))
    marker = tmp_path / 'bound.json'
    effects, requests, children, failures = [], [], [], []

    def owner_peer(ws):
        try:
            for raw in ws:
                request = json.loads(raw)
                if request['method'] == 'worker.adopt':
                    effects.append(request)
                    result = {'owner_epoch': 1}
                else:
                    assert request['method'] == 'kanban.run'
                    assert 'hermes-gateway-ticket.fixture-ticket' in ws.request.headers['Sec-WebSocket-Protocol']
                    assert request['params'] == {**params, 'db': context['db']}
                    requests.append(request)
                    env = {'PATH': os.defpath, 'HOME': str(tmp_path), 'HERMES_HOME': str(home),
                        'TMPDIR': str(tmp_path), 'LANG': 'C.UTF-8', 'FIXTURE_BOUND': str(marker),
                        'FIXTURE_RPC_URL': f'ws://127.0.0.1:{server.socket.getsockname()[1]}'}
                    child = placing_spawn(subprocess.Popen, policy)(contract.worker_command(policy),
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        cwd=tmp_path, env=env, umask=0o002)
                    children.append(child)
                    with selectors.DefaultSelector() as ready:
                        ready.register(child.stdout, selectors.EVENT_READ)
                        assert ready.select(30), 'native hello deadline'
                    hello = json.loads(child.stdout.readline())
                    assert hello['type'] == 'hello'
                    frame['scope'].update(pid=hello['pid'], birth=hello['birth'])
                    out, err = child.communicate(lifecycle.encode(frame), timeout=40)
                    assert child.returncode == 1, err.decode(errors='replace')
                    assert b'fixture_after_observation' in err, err.decode(errors='replace')
                    assert [json.loads(line) for line in out.splitlines()] == [
                        {'type': 'error', 'reason': 'managed_worker_failed'}]
                    result = {'session_id': 'fixture-session', 'receipt': {
                        'status': 'unknown', 'admission_id': 'fixture-admission'}}
                ws.send(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}))
        except BaseException as exc:
            failures.append(exc)
            raise

    server = serve(owner_peer, '127.0.0.1', 0,
        select_subprotocol=lambda connection, protocols: (
            gateway_client.GATEWAY_WS_PROTOCOL if gateway_client.GATEWAY_WS_PROTOCOL in protocols else None))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = replace(binding._endpoint(binding.BASE_CAPS | {binding.CAPABILITY,
        'kanban-capsule-contract-v2'}, sha),
        api_origin=f'http://127.0.0.1:{server.socket.getsockname()[1]}')
    binding._discovery(monkeypatch, endpoint, [])
    monkeypatch.setattr('hermes_cli.gateway_runtime.ensure_gateway_runtime',
        lambda *args, **kw: GatewayDiscovery('ready', endpoint))
    monkeypatch.setattr(gateway_client, '_session_ticket', lambda *args, **kw: 'fixture-ticket')
    try:
        with pytest.raises(gateway_client.GatewayClientError, match='Kanban execution unknown; retained by owner'):
            asyncio.run(asyncio.wait_for(kanban_worker_client.run(params, context['db']), 60))
        assert failures == []
        assert len(requests) == len(children) == len(effects) == 1
        assert effects[0]['params']['pid'] == children[0].pid
        assert json.loads(marker.read_text()) == {'workspace': context['workspace'],
            'home': str(home), 'run': str(context['run_id'])}
        assert acceptor.payloads[0]['child_pid'] == children[0].pid
        acceptor.fds[0].settimeout(2)
        traffic = acceptor.fds[0].recv(65536)
        assert json.loads(traffic) == {'v': 1, 'op': 'bound', 'task_id': context['task_id'],
            'run_id': context['run_id'], 'pid': children[0].pid}
        assert (acceptor.dir / f"{context['run_id']}.consumed").exists()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        server.shutdown()
        thread.join(5)
        acceptor.close()
        assert not thread.is_alive()
