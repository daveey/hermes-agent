"""Composed native main/execute through real claim binding and observation.

Only owner RPC endpoint selection and the post-observation checkpoint effect are
fixture seams, committed into the synthetic core. No managed-worker code, identity
check, claim check or observation code is replaced. The observer is the existing
verified stand-in, not the private Ask Eve observer or a model execution proof.
"""
import json
import os
import shutil
import socket
import subprocess
import threading

import pytest

from tests.hermes_cli import test_code_attestation_route as helpers
from tests.agent.test_managed_worker_observer_load import encode, observer_message, stage_observer, trusted_base

core = helpers.core
python = helpers.python
pytestmark = helpers.pytestmark


@pytest.fixture(scope='module')
def lifecycle_core(core, tmp_path_factory):
    root = tmp_path_factory.mktemp('lifecycle-core') / 'core'
    shutil.copytree(core, root)
    (root / '.gitignore').write_text('__pycache__/\n*.pyc\n')
    rpc = root / 'agent/runtime_session_store.py'
    text = rpc.read_text()
    marker = '    def _connection(self):\n'
    assert text.count(marker) == 1
    text = text.replace(marker, marker + "        from websockets.sync.client import connect\n"
        "        if self.link is None:\n"
        "            self.link = _OwnerLink(connect(os.environ['FIXTURE_RPC_URL'], proxy=None))\n"
        "        return self.link\n", 1)
    rpc.write_text(text)
    marker = '    def __call__(self, method, **params):\n'
    assert text.count(marker) == 1
    rpc.write_text(text.replace(marker, marker +
        "        if os.environ.get('FIXTURE_LOSE_EXPECTATION') == '1':\n"
        "            from gateway import capsule_contract\n"
        "            capsule_contract._worker_expected_sha = None\n", 1))
    checkpoint = root / 'tools/process_registry_checkpoint.py'
    text = checkpoint.read_text()
    marker = '                                session_keys: Optional[Iterable[str]] = None) -> int:\n'
    assert text.count(marker) == 1
    checkpoint.write_text(text.replace(marker, marker +
        "        import json, os\n"
        "        from tools.capsule_workspace import current_binding\n"
        "        binding = current_binding()\n"
        "        from pathlib import Path\n"
        "        Path(os.environ['FIXTURE_BOUND']).write_text(json.dumps({\n"
        "            'workspace': binding.workspace, 'home': os.environ['HERMES_HOME'],\n"
        "            'run': binding.run_id}))\n"
        "        if os.environ.get('FIXTURE_UPDATE_AFTER_BIND') == '1':\n"
        "            Path(os.environ['FIXTURE_UPDATE_PATH']).write_text('VALUE = 1\\n')\n"
        "            return 0\n"
        "        raise RuntimeError('fixture_after_observation')\n", 1))
    helpers._git(root, 'add', '.')
    helpers._git(root, 'commit', '-qm', 'composed-fixture-seams')
    return root


@pytest.mark.parametrize('case', ['bound', 'update-after-hello', 'update-during-adopt',
                                'lost-during-adopt', 'wrong-start', 'update-after-bind'])
def test_native_execute_adopts_then_binds_or_refuses(tmp_path, lifecycle_core, python, monkeypatch, case):
    from websockets.sync.server import serve
    from tests.gateway.test_managed_turn_contract import assignment
    from tests.gateway.test_session_kanban_capsule_guard import _policy
    from gateway import capsule_contract as contract

    home = tmp_path / 'profile'
    home.mkdir()
    policy = _policy(home, monkeypatch)
    context = json.loads(policy.kanban_json)
    # Keep the safe, credential-free assignment while using the real claimed board context.
    frame, _ = assignment(home, monkeypatch)
    frame['policy'].update(source='kanban', kanban_json=policy.kanban_json)
    frame['scope'].update(profile_id=str(home), epoch=1)
    frame['home'] = str(home)
    frame['scope']['execution_id'] = 'capsule-fixture'
    effects = []

    def handle(ws):
        for raw in ws:
            request = json.loads(raw)
            effects.append(request)
            assert request['method'] == 'worker.adopt', request
            if case == 'update-during-adopt':
                changed.write_text('VALUE = 1\n')
            result = {'owner_epoch': 1}
            ws.send(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}))

    server = serve(handle, '127.0.0.1', 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    owner, child = socket.socketpair()
    child.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    observer = stage_observer(trusted_base(tmp_path))
    owner.sendall(encode(observer_message(observer)))
    start = {'v': 1, 'op': 'start', 'task_id': context['task_id'], 'run_id': context['run_id'],
             'claim_lock': context['claim_lock'], 'workspace': context['workspace']}
    if case == 'wrong-start':
        start['run_id'] += 1
    owner.sendall(encode(start))
    sha = helpers._git(lifecycle_core, 'rev-parse', 'HEAD').strip()
    monkeypatch.setattr(contract, 'CORE_ROOT', lifecycle_core)
    monkeypatch.setattr('gateway.session_kanban_capsule.check_capsule_transport', lambda policy: True)
    argv = contract.worker_command(policy) + ['--observation-fd', str(child.fileno()), contract.WORKER_FLAG, sha]
    marker = tmp_path / 'bound.json'
    env = {'PATH': os.defpath, 'HOME': str(tmp_path), 'HERMES_HOME': str(home),
           'TMPDIR': str(tmp_path), 'LANG': 'C.UTF-8', 'FIXTURE_BOUND': str(marker),
           'FIXTURE_UPDATE_AFTER_BIND': '1' if case == 'update-after-bind' else '0',
           'FIXTURE_UPDATE_PATH': str(lifecycle_core / 'startup_update_fixture.py'),
           'FIXTURE_LOSE_EXPECTATION': '1' if case == 'lost-during-adopt' else '0',
           'FIXTURE_RPC_URL': f'ws://127.0.0.1:{server.socket.getsockname()[1]}'}
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=tmp_path, pass_fds=(child.fileno(),), env=env, umask=0o002)
    child.close()
    changed = lifecycle_core / 'startup_update_fixture.py'
    try:
        # A real native hello proves initial identity/argv acceptance before an update.
        import selectors
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(30), 'native hello deadline'
        hello = json.loads(process.stdout.readline())
        assert hello['type'] == 'hello'
        frame['scope'].update(pid=hello['pid'], birth=hello['birth'])
        if case == 'update-after-hello':
            changed.write_text('VALUE = 1\n')
        out, err = process.communicate(encode(frame), timeout=40)
        assert process.returncode == 1, err.decode(errors='replace')
        assert [json.loads(line) for line in out.splitlines()] == [
            {'type': 'error', 'reason': 'managed_worker_failed'}]
        owner.settimeout(1)
        traffic = b''
        try:
            while chunk := owner.recv(4096):
                traffic += chunk
        except (TimeoutError, ConnectionResetError):
            pass
        if case == 'update-after-hello':
            assert b'capsule_worker_code_unverified' in err
            assert effects == [] and not marker.exists() and traffic == b''
            assert not (home / 'worker-outboxes').exists()
        else:
            assert [item['method'] for item in effects] == ['worker.adopt']
            assert effects[0]['params']['pid'] == hello['pid']
            outbox = home / 'worker-outboxes/capsule-fixture'
            assert outbox.stat().st_mode & 0o777 == 0o700
            assert (outbox / 'pending.json').stat().st_mode & 0o777 == 0o600
            assert (outbox / 'owner.lock').stat().st_mode & 0o777 == 0o600
            if case in ('update-during-adopt', 'lost-during-adopt'):
                assert b'capsule_worker_code_unverified' in err
                assert not marker.exists() and traffic == b''
            elif case == 'wrong-start':
                assert b'capsule_observation_start_mismatch' in err
                assert not marker.exists() and traffic == b''
            else:
                expected = (b'capsule_worker_code_unverified' if case == 'update-after-bind'
                            else b'fixture_after_observation')
                assert expected in err, err.decode(errors='replace')
                assert json.loads(marker.read_text()) == {'workspace': context['workspace'],
                    'home': str(home), 'run': str(context['run_id'])}
                messages = [json.loads(line) for line in traffic.splitlines()]
                assert messages == [{'v': 1, 'op': 'bound', 'task_id': context['task_id'],
                    'run_id': context['run_id'], 'pid': hello['pid']}]
                # Deliberate checkpoint failure must poison, never emit a clean close.
                assert b'"close"' not in traffic
    finally:
        if changed.exists():
            changed.unlink()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        owner.close()
        server.shutdown()
        thread.join(5)
        assert not thread.is_alive()
    assert helpers._git(lifecycle_core, 'status', '--porcelain').strip() == ''
