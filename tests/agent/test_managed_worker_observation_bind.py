"""Managed worker binds its capsule from the owner-brokered observation channel before any tool import. [daveey]

``observation_bind(frame)`` runs right after ``bind_worker_context`` and before checkpoint recovery, worker
hooks, MCP discovery and AIAgent. No owner-installed channel: a no-op for ordinary cards, a refusal for a
capsule-required card. A channel whose start never arrives, is malformed, names another attempt or was not
created by an ancestor (the owner) refuses before tool discovery: fail closed, never skipped.
"""
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.agent.test_managed_worker_observer_load import (OBSERVER_NAMES, encode, observer_message, stage_observer,
                                                           trusted_base)
from tests.gateway.test_managed_turn_contract import assignment

ROOT = str(Path(__file__).resolve().parents[2])
pytestmark = pytest.mark.platforms('linux')


@pytest.fixture(autouse=True)
def _clean_channel():
    from agent import observation_bind as ob
    ob._reset_for_tests()
    saved = {n: sys.modules.pop(n) for n in OBSERVER_NAMES if n in sys.modules}
    yield
    ob._reset_for_tests()
    for name in OBSERVER_NAMES:  # run22: an in-process bind loads the staged observer modules
        sys.modules.pop(name, None)
    sys.modules.update(saved)


def _context(tmp_path, *, required=True):
    workspace = Path(os.path.realpath(tmp_path)) / 'ws'
    (workspace / 'sandbox').mkdir(parents=True, exist_ok=True)
    return {'task_id': 't_obs', 'run_id': 7, 'claim_lock': 'host:1', 'db': str(tmp_path / 'kanban.db'),
            'board': 'b', 'workspace': str(workspace),
            'capsule': {'required': True, 'requires': 'host_repo', 'backend': 'remote'} if required
            else {'required': False}}


def _start(context, **override):
    return json.dumps({'v': 1, 'op': 'start', 'task_id': context['task_id'], 'run_id': context['run_id'],
                       'claim_lock': context['claim_lock'], 'workspace': context['workspace'] + '/sandbox',
                       **override}).encode() + b'\n'


def test_argv_flag_is_strict():
    from agent import observation_bind as ob
    ob.install_from_argv([])
    assert ob.installed_fd() is None
    for bad in (['--observation-fd'], ['--observation-fd', 'x'], ['--observation-fd', '-1'], ['--other', '3'],
                ['--observation-fd', '3', 'extra']):
        with pytest.raises(ValueError, match='invalid_managed_worker_argv'):
            ob.install_from_argv(bad)


def test_no_channel_ordinary_card_is_a_noop(tmp_path, monkeypatch):
    from agent.observation_bind import observation_bind
    called = []
    monkeypatch.setattr('tools.capsule_workspace.bind_worker_workspace', lambda *a, **k: called.append(a))
    frame = {'policy': {'source': 'kanban', 'kanban_json': json.dumps(_context(tmp_path, required=False))}}
    assert observation_bind(frame) is None
    assert observation_bind({'policy': {'kanban_json': None}}) is None
    assert called == []


def test_no_channel_required_card_refuses(tmp_path):
    from agent.observation_bind import observation_bind
    frame = {'policy': {'source': 'kanban', 'kanban_json': json.dumps(_context(tmp_path))}}
    with pytest.raises(RuntimeError, match='capsule_observation_missing'):
        observation_bind(frame)


CHILD = r'''
import json, os, sys
sys.path.insert(0, {root!r})
from agent import observation_bind as ob
mode = sys.argv[1]
argv = sys.argv[2:]
if mode == 'own':
    import socket
    a, b = socket.socketpair()
    argv = ['--observation-fd', str(b.fileno())]
    a.sendall(sys.stdin.buffer.readline())
ob.install_from_argv(argv)
frame = {{'policy': {{'source': 'kanban', 'kanban_json': os.environ['CTX']}}, 'home': os.environ['HERMES_HOME']}}
try:
    binding = ob.observation_bind(frame)
except Exception as exc:
    print(json.dumps({{'error': str(exc)}}), flush=True)
    raise SystemExit(3)
from tools.capsule_workspace import current_binding
print(json.dumps({{'bound': current_binding() is binding, 'workspace': binding.workspace, 'run': binding.run_id}}), flush=True)
ob.observation_revoke()
from tools.capsule_workspace import CapsuleWorkspaceError
try:
    current_binding()
    revoked = False
except CapsuleWorkspaceError:
    revoked = True
print(json.dumps({{'revoked': revoked}}), flush=True)
'''


def _child(tmp_path, context, mode, payload, *, deadline='5'):
    home = Path(os.path.realpath(tmp_path)) / 'home'
    home.mkdir(exist_ok=True)
    env = dict(os.environ, HERMES_HOME=str(home), CTX=json.dumps(context), HERMES_KANBAN_TASK=context['task_id'],
               HERMES_KANBAN_RUN_ID=str(context['run_id']), PYTHONPATH=ROOT)
    owner, child_end = socket.socketpair()
    # run22: the owner enables SO_PASSCRED on the child's end and writes the trusted observer line first.
    child_end.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    owner.sendall(encode(observer_message(stage_observer(trusted_base(tmp_path)))))
    args = [sys.executable, '-c', CHILD.format(root=ROOT), mode]
    kwargs = dict(env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=ROOT)
    if mode == 'inherit':
        args += ['--observation-fd', str(child_end.fileno())]
        kwargs['pass_fds'] = (child_end.fileno(),)
    process = subprocess.Popen(args, **kwargs)
    child_end.close()
    try:
        if mode == 'inherit':
            if payload is not None:
                owner.sendall(payload)
            process.stdin.close()
        else:
            process.stdin.write(payload)
            process.stdin.close()
        out = process.stdout.read().decode().splitlines()
        process.wait(timeout=20)
        acks = b''
        owner.settimeout(0.5)
        try:
            acks = owner.recv(4096)
        except OSError:
            pass
        return process.returncode, [json.loads(line) for line in out], acks
    finally:
        owner.close()
        process.stdout.close()


def test_owner_brokered_channel_binds_workspace_real_child(tmp_path):
    context = _context(tmp_path)
    rc, out, acks = _child(tmp_path, context, 'inherit', _start(context))
    assert rc == 0, out
    assert out[0] == {'bound': True, 'workspace': context['workspace'] + '/sandbox', 'run': '7'}
    assert out[1] == {'revoked': True}
    assert json.loads(acks.split(b'\n')[0]) == {'v': 1, 'op': 'bound', 'task_id': 't_obs', 'run_id': 7,
                                                'pid': json.loads(acks.split(b'\n')[0])['pid']}


@pytest.mark.parametrize('override,reason', [
    ({'run_id': 8}, 'capsule_observation_start_mismatch'),
    ({'claim_lock': 'other:1'}, 'capsule_observation_start_mismatch'),
    ({'task_id': 't_other'}, 'capsule_observation_start_mismatch'),
    ({'op': 'hello'}, 'capsule_observation_start_malformed'),
    ({'workspace': '/etc'}, 'capsule_observation_workspace'),
])
def test_wrong_start_refuses_real_child(tmp_path, override, reason):
    context = _context(tmp_path)
    rc, out, _ = _child(tmp_path, context, 'inherit', _start(context, **override))
    assert rc == 3 and out == [{'error': reason}]


def test_malformed_start_line_refuses_real_child(tmp_path):
    context = _context(tmp_path)
    rc, out, _ = _child(tmp_path, context, 'inherit', b'not json\n')
    assert rc == 3 and out == [{'error': 'capsule_observation_start_malformed'}]


def test_closed_channel_without_start_refuses_real_child(tmp_path):
    context = _context(tmp_path)
    rc, out, _ = _child(tmp_path, context, 'inherit', None)
    assert rc == 3 and out == [{'error': 'capsule_observation_start_missing'}]


def test_self_made_channel_is_not_owner_supplied(tmp_path):
    """A socketpair the worker made itself (peer = this pid, not an ancestor) is refused."""
    context = _context(tmp_path)
    rc, out, _ = _child(tmp_path, context, 'own', _start(context))
    assert rc == 3 and out == [{'error': 'capsule_observation_peer'}]


def _kanban_frame(tmp_path, monkeypatch, context):
    frame, _ = assignment(tmp_path, monkeypatch)
    frame = {**frame, 'policy': {**frame['policy'], 'source': 'kanban', 'kanban_json': json.dumps(context)}}
    return frame


def _execute(tmp_path, monkeypatch, events, context, *, start=None, agent_error=None, recovery_error=None):
    # In-process observer unit fixture: argv/identity are proved by native lifecycle tests.
    monkeypatch.setattr('gateway.capsule_contract._worker_expected_sha', 'unit-owner')
    monkeypatch.setattr('gateway.capsule_contract._verified_sha', lambda **kwargs: 'unit-owner')
    from agent import managed_worker as worker
    from agent import observation_bind as ob

    class RPC:
        def __init__(self, home): pass
        def __call__(self, method, **params): return {'owner_epoch': 1}
        def close(self): events.append('rpc.close')

    class Agent:
        def __init__(self, **kwargs):
            events.append('agent')
            raise agent_error or RuntimeError('stop_here')

    class Store:
        def __init__(self, *args): pass
        def close(self): events.append('store.close')

    class Controls:
        def __init__(self, *args):
            self.stopped, self.finish = threading.Event(), threading.Event()
        def approval(self, data): pass
        def clarify(self, questions): pass

    class Binding:
        def close(self):
            events.append('revoke')

    def bind_context(frame):
        events.append('bind_context')
        monkeypatch.setenv('HERMES_KANBAN_TASK', context['task_id'])
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(context['run_id']))

    def bind_workspace(task_id, run_id, home, workspace, is_live):
        events.append(('bind_workspace', task_id, run_id, workspace, is_live()))
        return Binding()

    frame = _kanban_frame(tmp_path, monkeypatch, context)
    from gateway import session_policy
    restore, source = session_policy.restore_policy, assignment(tmp_path, monkeypatch)[0]['policy']['source']
    monkeypatch.setattr(session_policy, 'restore_policy',
                        lambda data: restore({**data, 'source': source, 'kanban_json': None}))
    monkeypatch.setattr(worker, 'bind_worker_policy', lambda frame: None)
    monkeypatch.setattr(worker, 'discover_profile_mcp', lambda policy: events.append('discover'))
    monkeypatch.setattr(worker, 'retire_agent', lambda agent: None)
    monkeypatch.setattr(worker, 'WorkerControls', Controls)
    monkeypatch.setattr('gateway.session_kanban.bind_worker_context', bind_context)
    monkeypatch.setattr('gateway.session_policy.register_worker_hooks', lambda policy: events.append('hooks'))
    monkeypatch.setattr('agent.runtime_session_store.WorkerRPC', RPC)
    monkeypatch.setattr('agent.runtime_session_store.RuntimeSessionStore', Store)
    def recover(**kwargs):
        events.append('recover')
        if recovery_error is not None:
            raise recovery_error
    monkeypatch.setattr('tools.process_registry.process_registry.recover_from_checkpoint', recover)
    monkeypatch.setattr('run_agent.AIAgent', Agent)
    monkeypatch.setattr('tools.capsule_workspace.bind_worker_workspace', bind_workspace)
    # In-process the socketpair's creator is this pid, not an ancestor; the real ancestry check is
    # exercised by the subprocess tests above.
    monkeypatch.setattr(ob, '_peer_is_owner', lambda sock: True)
    monkeypatch.setattr(ob, 'START_DEADLINE_S', 0.3)
    owner, child_end = socket.socketpair()
    child_end.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)  # run22: owner-written observer line first
    owner.sendall(encode(observer_message(stage_observer(trusted_base(tmp_path)))))
    ob.install_from_argv(['--observation-fd', str(child_end.detach())])
    if start is not None:
        owner.sendall(start)
    try:
        worker.execute(frame, SimpleNamespace(send=lambda kind, **payload: events.append(kind)))
    finally:
        owner.close()


def test_execute_binds_workspace_after_context_before_recovery_hooks_and_mcp(tmp_path, monkeypatch):
    context = _context(tmp_path)
    events = []
    with pytest.raises(RuntimeError, match='stop_here'):
        _execute(tmp_path, monkeypatch, events, context, start=_start(context))
    names = [e[0] if isinstance(e, tuple) else e for e in events]
    assert names[:5] == ['bind_context', 'bind_workspace', 'recover', 'hooks', 'discover']
    assert events[1] == ('bind_workspace', 't_obs', '7', context['workspace'] + '/sandbox', True)
    assert 'revoke' in names[names.index('agent'):]  # revoked in execute's finally


def test_execute_checkpoint_failure_revokes_and_closes_before_discovery(tmp_path, monkeypatch):
    context = _context(tmp_path)
    events = []
    with pytest.raises(RuntimeError, match='checkpoint_failed'):
        _execute(tmp_path, monkeypatch, events, context, start=_start(context),
                 recovery_error=RuntimeError('checkpoint_failed'))
    names = [e[0] if isinstance(e, tuple) else e for e in events]
    # Observer failure-finalization and unconditional revocation both tombstone
    # the idempotent binding; neither may bypass store/RPC cleanup.
    assert names == ['bind_context', 'bind_workspace', 'recover', 'revoke', 'revoke', 'store.close', 'rpc.close']


def test_execute_start_never_arrives_raises_before_tool_discovery(tmp_path, monkeypatch):
    context = _context(tmp_path)
    events = []
    with pytest.raises(RuntimeError, match='capsule_observation_start_missing'):
        _execute(tmp_path, monkeypatch, events, context, start=None)
    assert events == ['bind_context', 'store.close', 'rpc.close']


def test_execute_malformed_start_raises_before_tool_discovery(tmp_path, monkeypatch):
    context = _context(tmp_path)
    events = []
    with pytest.raises(RuntimeError, match='capsule_observation_start_malformed'):
        _execute(tmp_path, monkeypatch, events, context, start=b'{"v":1}\n')
    assert events == ['bind_context', 'store.close', 'rpc.close']


def test_main_refuses_bad_observation_argv_before_hello():
    """``main`` installs the owner-only flag first: a bad argv fails before the hello frame (real process)."""
    process = subprocess.run([sys.executable, '-m', 'agent.managed_worker', '--observation-fd', 'nope'],
                             cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
                             env=dict(os.environ, PYTHONPATH=ROOT), check=False)
    assert process.returncode == 1
    assert b'hello' not in process.stdout and b'managed_worker_failed' in process.stdout
