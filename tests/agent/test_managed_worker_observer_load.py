"""Owner-pinned trusted observer load in the managed worker (capsule-required cards). [daveey]

The owner writes one ``observer`` line ({root, digest, host_pid}, taken from the verified acceptor
registration) into the child's end of the brokered socketpair before it hands the peer end to the
dispatcher host. ``observation_bind`` must read it from the owner (SCM_CREDENTIALS pid == the verified
owner ancestor), verify the root (canonical, owned by this uid, not group/world-writable, outside every
workspace), recompute the digest over exactly ``observer_{codex,runtime,wire}.py``, refuse on core hook
drift, load those exact bytes under their module names (never sys.path / cwd), and only then read
``start`` (sender pid == host_pid). After the bind the observer is installed on a dup of the channel and
the capsule binding's liveness goes through its authenticated ``workspace_check``. ``execute`` seals on
success (observer ``close``) and leaves a failed run poisoned and unsealed. Ordinary cards never import
any of it.

The observer modules here are small stand-ins staged in a private temp dir (core never imports the
private integration runtime); the real modules are exercised end to end by integration/
test_observer_acceptor.py.
"""
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = str(Path(__file__).resolve().parents[2])
pytestmark = pytest.mark.platforms('linux')

OBSERVER_NAMES = ('observer_wire', 'observer_runtime', 'observer_codex')
# Shared golden vector: integration/test_observer_acceptor.py pins the same value for its own copy.
GOLDEN_FILES = {'observer_wire.py': b'wire\n', 'observer_runtime.py': b'runtime\n', 'observer_codex.py': b'codex\n'}
GOLDEN_DIGEST = '463be305bd45d19707133e7366e3dec8912347ba82b3761bec8fbdd9734cd841'

FAKE_WIRE = '''\
VERSION = 1


def require(ok, reason):
    if not ok:
        raise PermissionError(reason)
'''

FAKE_RUNTIME = '''\
"""Stand-in for the private observer_runtime: same install/active/finish_oneshot surface."""
import json
import socket
from observer_wire import require

_channel = None
CHECKS = []


class Observer:
    def __init__(self, fd, run_id, host_pid):
        self.sock = socket.socket(fileno=fd)
        self.run_id, self.host_pid = run_id, host_pid
        self.capsule = self.binding = None
        self.failed = self.closing = False

    def workspace_check(self):
        require(self.capsule is not None and not self.closing and not self.failed, 'capsule channel not live')
        CHECKS.append(dict(self.capsule))
        return True

    def revoke(self):
        binding, self.binding = self.binding, None
        if binding is not None:
            binding.close()

    def close(self):
        require(not self.failed and not self.closing, 'unflushed/poisoned observation')
        self.closing = True
        self.revoke()
        self.sock.sendall(json.dumps({'op': 'close', 'run_id': self.run_id}).encode() + b'\\n')
        self.sock.close()


def install(fd, run_id, host_pid):
    global _channel
    require(_channel is None, 'observer already installed')
    _channel = Observer(fd, run_id, host_pid)
    return _channel


def active():
    return _channel is not None


def finish_oneshot(rc):
    code = rc if isinstance(rc, int) else 1
    if _channel is None:
        return code
    if code != 0:
        _channel.failed = True
    else:
        try:
            _channel.close()
        except BaseException:
            _channel.failed = True
            code = 1
    _channel.revoke()
    return code
'''

FAKE_CODEX = '''\
import observer_runtime as runtime
from observer_wire import require
'''

POISON = '''\
import os
open(os.environ['POISON'], 'w').write(__file__)
raise ImportError('poisoned observer module imported from sys.path/cwd')
'''


def digest(files):
    """Test-side copy of the shared digest definition (pinned by GOLDEN_DIGEST)."""
    h = hashlib.sha256()
    for name in sorted(files):
        data = files[name]
        h.update(name.encode('utf-8') + b'\x00' + len(data).to_bytes(8, 'big') + data)
    return h.hexdigest()


def stage_observer(base):
    """A trusted observer root: owner-only writable dir + files (what the dispatcher host installs)."""
    root = Path(os.path.realpath(base)) / 'observer-root'
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o755)
    for name, text in (('observer_wire.py', FAKE_WIRE), ('observer_runtime.py', FAKE_RUNTIME),
                       ('observer_codex.py', FAKE_CODEX)):
        path = root / name
        path.write_text(text)
        os.chmod(path, 0o644)
    return root


def root_digest(root):
    return digest({n: (Path(root) / n).read_bytes() for n in GOLDEN_FILES})


def _chain(path):
    real = os.path.realpath(path)
    parts = real.split(os.sep)[1:]
    return [(os.sep + os.sep.join(parts[:i]), os.lstat(os.sep + os.sep.join(parts[:i])))
            for i in range(len(parts) + 1)]


def trusted_base(tmp_path):
    """A per-test dir whose whole ancestor chain passes the production ancestor rule.

    The test runner's basetemp root can be a group-writable own dir (umask 002), which the managed child
    rightly refuses as an observer location; the dispatcher host installs the real observer in a private
    dir, so stage it in a compliant temp location instead (tmp_path itself when it already complies).
    """
    import atexit
    import shutil
    import tempfile
    from agent.observation_load import ancestors_problem
    for candidate in (str(tmp_path), tempfile.gettempdir(), '/var/tmp'):  # no-tmp: ok — FHS root-owned sticky fallback for a compliant chain
        if os.path.isdir(candidate) and ancestors_problem(_chain(candidate)) is None:
            base = tempfile.mkdtemp(prefix='observer-trusted-', dir=candidate)
            atexit.register(shutil.rmtree, base, True)
            return Path(base)
    pytest.skip('no temp location with a private ancestor chain on this host')


def observer_message(root, host_pid=None, **override):
    message = {'v': 1, 'op': 'observer', 'root': str(root), 'digest': root_digest(root),
               'host_pid': os.getpid() if host_pid is None else host_pid}
    message.update(override)
    return message


def encode(message):
    return json.dumps(message, sort_keys=True).encode() + b'\n'


def _context(tmp_path):
    workspace = Path(os.path.realpath(tmp_path)) / 'ws'
    (workspace / 'sandbox').mkdir(parents=True, exist_ok=True)
    return {'task_id': 't_obs', 'run_id': 7, 'claim_lock': 'host:1', 'db': str(tmp_path / 'kanban.db'),
            'board': 'b', 'workspace': str(workspace),
            'capsule': {'required': True, 'requires': 'host_repo', 'backend': 'remote'}}


def _start(context, **override):
    return json.dumps({'v': 1, 'op': 'start', 'task_id': context['task_id'], 'run_id': context['run_id'],
                       'claim_lock': context['claim_lock'], 'workspace': context['workspace'] + '/sandbox',
                       **override}).encode() + b'\n'


CHILD = r'''
import json, os, sys
sys.path.insert(0, {root!r})
cfg = json.loads(os.environ['OBS_CFG'])
from agent import observation_bind as ob
if cfg.get('drift'):
    from agent import observation_load
    observation_load.SENTINELS = tuple(observation_load.SENTINELS) + (('agent.managed_worker', 'no such hook in core'),)
ob.install_from_argv(sys.argv[1:])
frame = {{'policy': {{'source': 'kanban', 'kanban_json': os.environ['CTX']}}, 'home': os.environ['HERMES_HOME']}}
from agent import observation_seam
before = observation_seam.active()
try:
    binding = ob.observation_bind(frame)
except Exception as exc:
    print(json.dumps({{'error': str(exc), 'loaded': 'observer_runtime' in sys.modules,
                      'active': observation_seam.active()}}), flush=True)
    raise SystemExit(3)
rt = sys.modules['observer_runtime']
from tools.capsule_workspace import current_binding
live = current_binding() is binding
print(json.dumps({{'before': before, 'active': observation_seam.active(), 'live': live, 'checks': rt.CHECKS,
                  'capsule': rt._channel.capsule, 'host_pid': rt._channel.host_pid, 'run_id': rt._channel.run_id,
                  'files': {{n: sys.modules[n].__file__ for n in ('observer_wire', 'observer_runtime', 'observer_codex')}},
                  'wire_is_verified': rt.require is sys.modules['observer_wire'].require,
                  'codex_runtime': sys.modules['observer_codex'].runtime is rt,
                  'poison': os.path.exists(os.environ['POISON']),
                  'binding_is_observer': rt._channel.binding is binding,
                  'path_has_root': cfg['root'] in sys.path or cfg['root'] + '/' in sys.path}}), flush=True)
rc = ob.observation_finish(cfg.get('ok', True))
again = ob.observation_finish(cfg.get('ok', True))
print(json.dumps({{'sealed': rc, 'again': again}}), flush=True)
'''


def _poison_dir(tmp_path):
    poison = tmp_path / 'poison'
    poison.mkdir(exist_ok=True)
    for name in OBSERVER_NAMES:
        (poison / f'{name}.py').write_text(POISON)
    return poison


def _run(tmp_path, context, *, line=None, start=None, writer='owner', cfg=None, root=None):
    """Spawn a real child holding the channel; this process is the owner (an ancestor) and the host."""
    home = Path(os.path.realpath(tmp_path)) / 'home'
    home.mkdir(exist_ok=True)
    poison = _poison_dir(tmp_path)
    marker = tmp_path / 'poisoned'
    env = dict(os.environ, HERMES_HOME=str(home), CTX=json.dumps(context), HERMES_KANBAN_TASK=context['task_id'],
               HERMES_KANBAN_RUN_ID=str(context['run_id']), PYTHONPATH=os.pathsep.join([ROOT, str(poison)]),
               POISON=str(marker), OBS_CFG=json.dumps(dict(cfg or {}, root=str(root or ''))))
    owner, child_end = socket.socketpair()
    child_end.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)  # what the owner's prepare() does
    if line is not None and writer == 'owner':
        owner.sendall(line)
    elif line is not None and writer == 'sibling':
        # A same-uid process that is NOT the worker's ancestor writes the line into the channel.
        subprocess.run([sys.executable, '-c', 'import os,sys; os.write(int(sys.argv[1]), sys.stdin.buffer.read())',
                        str(owner.fileno())], input=line, pass_fds=(owner.fileno(),), check=True, timeout=30)
    fd = child_end.fileno()
    process = subprocess.Popen([sys.executable, '-c', CHILD.format(root=ROOT), '--observation-fd', str(fd)],
                               pass_fds=(fd,), env=env, stdout=subprocess.PIPE, cwd=str(poison))
    child_end.close()
    try:
        if start is not None:
            owner.sendall(start)
        else:
            owner.shutdown(socket.SHUT_WR)  # nothing more will come: EOF, not a deadline wait
        out = process.stdout.read().decode().splitlines()
        process.wait(timeout=60)
        owner.settimeout(0.5)
        received = b''
        try:
            while chunk := owner.recv(65536):
                received += chunk
        except OSError:
            pass
        lines = [json.loads(x) for x in received.split(b'\n') if x]
        return process.returncode, [json.loads(x) for x in out], lines, marker.exists()
    finally:
        owner.close()
        process.stdout.close()


def test_digest_golden_vector_shared_with_integration():
    from agent.observation_load import observer_digest
    assert digest(GOLDEN_FILES) == GOLDEN_DIGEST
    assert observer_digest(GOLDEN_FILES) == GOLDEN_DIGEST
    assert observer_digest(dict(GOLDEN_FILES, **{'observer_wire.py': b'wire!\n'})) != GOLDEN_DIGEST


def test_verified_observer_loaded_installed_and_sealed_real_child(tmp_path):
    context = _context(tmp_path)
    root = stage_observer(trusted_base(tmp_path))
    rc, out, lines, poisoned = _run(tmp_path, context, line=encode(observer_message(root)), start=_start(context),
                                    root=root)
    assert rc == 0, out
    first, sealed = out
    home = os.path.realpath(tmp_path / 'home')
    capsule = {'task_id': 't_obs', 'run_id': '7', 'profile_home': home, 'workspace': context['workspace'] + '/sandbox'}
    assert (first['before'], first['active'], first['live']) == (False, True, True)
    assert first['checks'] == [capsule]  # liveness after the bind = the observer's authenticated workspace_check
    assert (first['capsule'], first['host_pid'], first['run_id']) == (capsule, os.getpid(), 7)
    assert first['files'] == {n: str(root / f'{n}.py') for n in OBSERVER_NAMES}
    assert first['wire_is_verified'] and first['codex_runtime'] and first['binding_is_observer']
    assert not first['poison'] and not poisoned and not first['path_has_root']
    assert sealed == {'sealed': 0, 'again': None}
    assert lines[0] == {'v': 1, 'op': 'bound', 'task_id': 't_obs', 'run_id': 7, 'pid': lines[0]['pid']}
    assert lines[1:] == [{'op': 'close', 'run_id': 7}]


def test_failed_run_is_poisoned_and_unsealed_real_child(tmp_path):
    context = _context(tmp_path)
    root = stage_observer(trusted_base(tmp_path))
    rc, out, lines, _ = _run(tmp_path, context, line=encode(observer_message(root)), start=_start(context),
                             root=root, cfg={'ok': False})
    assert rc == 0, out
    assert out[1] == {'sealed': 1, 'again': None}
    assert [x['op'] for x in lines] == ['bound']  # no close: unsealed


def _symlink_file(root):
    real = root.parent / 'wire-real.py'
    real.write_text(FAKE_WIRE)
    os.chmod(real, 0o644)
    (root / 'observer_wire.py').unlink()
    (root / 'observer_wire.py').symlink_to(real)


def _symlink_root(root, tmp_path):
    link = tmp_path / 'trusted-link'
    link.symlink_to(root, target_is_directory=True)
    return str(link)


# kind -> fs mutation of the staged root; returns a replacement ``root`` for the line, or None to keep it.
_SETUP = {
    'digest_mismatch': lambda c, root, tmp: (root / 'observer_runtime.py').write_text(
        FAKE_RUNTIME + '\n# edited after registration\n') and None,
    'root_group_writable': lambda c, root, tmp: os.chmod(root, 0o775),
    'root_world_writable': lambda c, root, tmp: os.chmod(root, 0o757),
    'file_group_writable': lambda c, root, tmp: os.chmod(root / 'observer_wire.py', 0o664),
    'missing_file': lambda c, root, tmp: (root / 'observer_codex.py').unlink(),
    'file_symlink': lambda c, root, tmp: _symlink_file(root),
    'root_in_card_workspace': lambda c, root, tmp: stage_observer(Path(c['workspace']) / 'tools'),
    'root_in_start_workspace': lambda c, root, tmp: stage_observer(Path(c['workspace']) / 'sandbox' / 'tools'),
    'root_in_workspaces_root': lambda c, root, tmp: stage_observer(tmp / 'home' / 'kanban' / 'workspaces' / 't_other'),
    'symlinked_root': lambda c, root, tmp: _symlink_root(root, tmp),
    'relative_root': lambda c, root, tmp: 'observer-root',
}
# kind -> the exact line bytes the owner side writes (None: nothing at all).
_LINE = {
    'line_missing': lambda msg, c: None,
    'start_first': lambda msg, c: _start(c),
    'extra_key': lambda msg, c: encode(dict(msg, grant=True)),
    'missing_key': lambda msg, c: encode({k: v for k, v in msg.items() if k != 'host_pid'}),
    'wrong_op': lambda msg, c: encode(dict(msg, op='start')),
    'wrong_version': lambda msg, c: encode(dict(msg, v=2)),
    'digest_not_hex': lambda msg, c: encode(dict(msg, digest='Z' * 64)),
    'host_pid_bool': lambda msg, c: encode(dict(msg, host_pid=True)),
    'not_json': lambda msg, c: b'observer please\n',
}


def _negative(tmp_path, kind):
    """(line bytes, writer, cfg) for one refusal; every one must refuse before start is read or bound."""
    context = _context(tmp_path)
    root = stage_observer(trusted_base(tmp_path))
    msg = observer_message(root)
    replaced = _SETUP.get(kind, lambda c, r, t: None)(context, root, tmp_path)
    if isinstance(replaced, Path):
        msg = observer_message(replaced)  # a fully staged root in a forbidden location (digest valid)
    elif isinstance(replaced, str):
        msg = dict(msg, root=replaced)
    line = _LINE.get(kind, lambda m, c: encode(m))(msg, context)
    writer = 'sibling' if kind == 'sender_not_owner' else 'owner'
    cfg = {'drift': True} if kind == 'drift' else {}
    return context, root, line, writer, cfg


UNTRUSTED = 'capsule_observer_untrusted'
MISSING = 'capsule_observer_missing'


@pytest.mark.parametrize('kind,code', [
    ('digest_mismatch', UNTRUSTED), ('root_group_writable', UNTRUSTED), ('root_world_writable', UNTRUSTED),
    ('file_group_writable', UNTRUSTED), ('missing_file', UNTRUSTED),
    ('file_symlink', UNTRUSTED), ('root_in_card_workspace', UNTRUSTED), ('root_in_start_workspace', UNTRUSTED),
    ('root_in_workspaces_root', UNTRUSTED), ('symlinked_root', UNTRUSTED), ('relative_root', UNTRUSTED),
    ('sender_not_owner', UNTRUSTED), ('drift', 'capsule_observer_hook_drift'),
    ('line_missing', MISSING), ('start_first', MISSING), ('extra_key', MISSING), ('missing_key', MISSING),
    ('wrong_op', MISSING), ('wrong_version', MISSING), ('digest_not_hex', MISSING), ('host_pid_bool', MISSING),
    ('not_json', MISSING),
])
def test_untrusted_or_missing_observer_refuses_before_start_real_child(tmp_path, kind, code):
    context, root, line, writer, cfg = _negative(tmp_path, kind)
    # ``start`` is only sent when the line is not itself the start; the child must refuse first.
    rc, out, lines, poisoned = _run(tmp_path, context, line=line, writer=writer, cfg=cfg, root=root,
                                    start=None if kind in ('line_missing', 'start_first') else _start(context))
    assert rc == 3, out
    assert out == [{'error': code, 'loaded': False, 'active': False}]
    assert lines == [] and not poisoned  # never bound, nothing written back


def test_start_sender_not_host_pid_refuses_before_bind_real_child(tmp_path):
    context = _context(tmp_path)
    root = stage_observer(trusted_base(tmp_path))
    other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        rc, out, lines, _ = _run(tmp_path, context, line=encode(observer_message(root, host_pid=other.pid)),
                                 start=_start(context), root=root)
    finally:
        other.kill()
        other.wait(timeout=10)
    assert rc == 3, out
    assert out == [{'error': 'capsule_observation_start_sender', 'loaded': True, 'active': False}]
    assert lines == []


ORDINARY = r'''
import json, os, sys
sys.path.insert(0, {root!r})
from agent import observation_bind as ob
ob.install_from_argv(sys.argv[1:])
ctx = {{'task_id': 't', 'run_id': 1, 'claim_lock': 'c', 'workspace': '/tmp', 'capsule': {{'required': False}}}}
results = [ob.observation_bind({{'policy': {{'source': 'kanban', 'kanban_json': json.dumps(ctx)}}}}),
           ob.observation_bind({{'policy': {{'kanban_json': None}}}}), ob.observation_finish(True),
           ob.observation_finish(False)]
ob.observation_revoke()
print(json.dumps({{'results': results, 'fd': ob.installed_fd(),
                  'imported': sorted(n for n in ('observer_runtime', 'observer_wire', 'observer_codex',
                                                 'agent.observation_load') if n in sys.modules)}}))
'''


def test_ordinary_card_never_imports_observer_real_child(tmp_path):
    proc = subprocess.run([sys.executable, '-c', ORDINARY.format(root=ROOT)], cwd=ROOT, capture_output=True,
                          timeout=60, env=dict(os.environ, PYTHONPATH=ROOT), check=False)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert json.loads(proc.stdout) == {'results': [None, None, None, None], 'fd': None, 'imported': []}


def _st(mode, uid):
    return SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=uid)


def test_ancestor_rule_units():
    from agent.observation_load import ancestors_problem
    me = os.getuid()
    ok = [('/', _st(0o755, 0)), ('/home', _st(0o755, 0)), ('/home/u', _st(0o750, me))]
    assert ancestors_problem(ok) is None
    # root-owned sticky world-writable (/tmp) is fine; an unshielded own g+w dir is not
    assert ancestors_problem(ok[:1] + [('/tmp', _st(0o1777, 0))]) is None
    assert ancestors_problem(ok + [('/home/u/shared', _st(0o775, me))]) is not None
    assert ancestors_problem(ok + [('/home/u/open', _st(0o777, me))]) is not None
    # an ancestor nobody else may traverse (0700) shields a g+w dir beneath it
    assert ancestors_problem(ok + [('/home/u/.private', _st(0o700, me)), ('/home/u/.private/c', _st(0o775, me))]) is None
    # foreign owner, or a non-root-owned sticky world-writable dir, refuses
    assert ancestors_problem(ok + [('/home/u/x', _st(0o755, me + 1))]) is not None
    assert ancestors_problem(ok + [('/home/u/t', _st(0o1777, me))]) is not None


# ---- execute(): seal on success, poison on failure, in-process (owner = this process) ----

@pytest.fixture
def in_process(tmp_path, monkeypatch):
    from agent import observation_bind as ob
    ob._reset_for_tests()
    saved = {n: sys.modules.pop(n) for n in OBSERVER_NAMES if n in sys.modules}
    yield
    ob._reset_for_tests()
    for name in OBSERVER_NAMES:
        sys.modules.pop(name, None)
    sys.modules.update(saved)


def _execute(tmp_path, monkeypatch, events, *, fail, setup=None):
    # In-process observer unit fixture: argv/identity are proved by native lifecycle tests.
    monkeypatch.setattr('gateway.capsule_contract._worker_expected_sha', 'unit-owner')
    monkeypatch.setattr('gateway.capsule_contract._verified_sha', lambda **kwargs: 'unit-owner')
    from agent import managed_worker as worker
    from agent import observation_bind as ob
    from tests.gateway.test_managed_turn_contract import assignment
    context = _context(tmp_path)
    root = stage_observer(trusted_base(tmp_path))

    class RPC:
        def __init__(self, home): pass
        def __call__(self, method, **params): return {'owner_epoch': 1}
        def close(self): events.append('rpc.close')

    class Agent:
        def __init__(self, **kwargs):
            events.append('agent')
            if fail:
                raise RuntimeError('stop_here')

    class Store:
        def __init__(self, *args): pass
        def get_messages_as_conversation(self, sid): return []
        def flush_token_counts(self): pass
        def finish(self): events.append('store.finish')
        def close(self): events.append('store.close')

    class Controls:
        def __init__(self, *args):
            self.stopped, self.finish = threading.Event(), threading.Event()
            self.finish.set()
            self.agent = None
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
        events.append(('bind_workspace', is_live()))
        return Binding()

    frame, _ = assignment(tmp_path, monkeypatch)
    frame = {**frame, 'policy': {**frame['policy'], 'source': 'kanban', 'kanban_json': json.dumps(context)}}
    from gateway import session_policy
    restore, source = session_policy.restore_policy, assignment(tmp_path, monkeypatch)[0]['policy']['source']
    monkeypatch.setattr(session_policy, 'restore_policy',
                        lambda data: restore({**data, 'source': source, 'kanban_json': None}))
    monkeypatch.setattr(worker, 'bind_worker_policy', lambda frame: None)
    monkeypatch.setattr(worker, 'discover_profile_mcp', lambda policy: None)
    monkeypatch.setattr(worker, 'retire_agent', lambda agent: None)
    monkeypatch.setattr(worker, 'WorkerControls', Controls)
    monkeypatch.setattr('gateway.session_kanban.bind_worker_context', bind_context)
    monkeypatch.setattr('gateway.session_kanban.run_worker_turns', lambda agent, frame, history: {'final_response': 'ok'})
    monkeypatch.setattr('agent.title_generator.wait_for_title_upgrades', lambda: None)
    monkeypatch.setattr('gateway.session_policy.register_worker_hooks', lambda policy: None)
    monkeypatch.setattr('agent.runtime_session_store.WorkerRPC', RPC)
    monkeypatch.setattr('agent.runtime_session_store.RuntimeSessionStore', Store)
    monkeypatch.setattr('tools.process_registry.process_registry.recover_from_checkpoint', lambda **_k: None)
    monkeypatch.setattr('run_agent.AIAgent', Agent)
    monkeypatch.setattr('tools.capsule_workspace.bind_worker_workspace', bind_workspace)
    # In-process the socketpair's creator is this pid, not an ancestor (the real ancestry check is in the
    # subprocess tests); this process writes both the observer line and the start, as owner and host.
    monkeypatch.setattr(ob, '_peer_is_owner', lambda sock: True)
    owner, child_end = socket.socketpair()
    child_end.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    owner.sendall(encode(observer_message(root)) + _start(context))
    ob.install_from_argv(['--observation-fd', str(child_end.detach())])
    real_finish = ob.observation_finish
    monkeypatch.setattr(ob, 'observation_finish', lambda ok: events.append(('finish', ok)) or real_finish(ok))
    if setup is not None:
        setup(worker, Store)
    try:
        worker.execute(frame, SimpleNamespace(send=lambda kind, **payload: events.append(kind)))
    finally:
        owner.settimeout(0.5)
        received = b''
        try:
            while chunk := owner.recv(65536):
                received += chunk
        except OSError:
            pass
        owner.close()
        events.append(('wire', [json.loads(x)['op'] for x in received.split(b'\n') if x]))


@pytest.mark.parametrize('boundary', ['checkpoint', 'hooks', 'construct', 'history'])
def test_execute_refuses_drift_at_each_late_boundary(tmp_path, monkeypatch, in_process, boundary):
    """Distinct effects expose each late gate; earlier checks still see clean identity."""
    events = []

    def setup(worker, store):
        def drift(*args, **kwargs):
            events.append('drift')
            monkeypatch.setattr('gateway.capsule_contract._verified_sha', lambda **kwargs: None)
            return []
        if boundary == 'checkpoint':
            monkeypatch.setattr('tools.process_registry.process_registry.recover_from_checkpoint', drift)
            monkeypatch.setattr('gateway.session_policy.register_worker_hooks', lambda policy: events.append('hooks'))
        elif boundary == 'hooks':
            monkeypatch.setattr('gateway.session_policy.register_worker_hooks', drift)
        elif boundary == 'construct':
            original = worker._construct_agent
            def construct(*args, **kwargs):
                result = original(*args, **kwargs)
                drift()
                return result
            monkeypatch.setattr(worker, '_construct_agent', construct)
        else:
            monkeypatch.setattr(store, 'get_messages_as_conversation', drift)
        monkeypatch.setattr('gateway.session_kanban.run_worker_turns',
                            lambda *args: events.append('turns') or {'final_response': 'ok'})

    with pytest.raises(RuntimeError, match='^capsule_worker_code_unverified$'):
        _execute(tmp_path, monkeypatch, events, fail=False, setup=setup)
    assert events.count('drift') == 1
    assert 'hooks' not in events
    assert 'turns' not in events
    if boundary != 'history':
        assert 'ready' not in events
    if boundary in ('checkpoint', 'hooks'):
        assert 'agent' not in events
    assert 'store.close' in events and 'rpc.close' in events
    assert ('finish', False) in events
    assert events[-1] == ('wire', ['bound'])


def test_execute_success_seals_before_revoke(tmp_path, monkeypatch, in_process):
    events = []
    _execute(tmp_path, monkeypatch, events, fail=False)
    names = [e[0] if isinstance(e, tuple) else e for e in events]
    assert names.index('finished') < names.index('finish') < names.index('revoke')
    assert ('finish', True) in events
    assert events[-1] == ('wire', ['bound', 'close'])  # sealed: the observer emitted close
    assert sys.modules['observer_runtime']._channel.failed is False


def test_execute_failure_is_poisoned_and_unsealed(tmp_path, monkeypatch, in_process):
    events = []
    with pytest.raises(RuntimeError, match='stop_here'):
        _execute(tmp_path, monkeypatch, events, fail=True)
    assert ('finish', False) in events
    assert events[-1] == ('wire', ['bound'])  # no close
    assert sys.modules['observer_runtime']._channel.failed is True
    names = [e[0] if isinstance(e, tuple) else e for e in events]
    assert names.index('finish') < names.index('revoke')
