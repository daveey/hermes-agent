"""Authenticated, run-bound, single-use owner rendezvous for capsule-required Kanban cards. [daveey]

A real in-test acceptor (a unix-socket server thread) stands in for the trusted dispatcher host. The owner
brokers one end of a socketpair to it over SCM_RIGHTS after spawning a real tiny python child that holds
the other end. Every failure is fail-closed: no handshake, no child; a failure after the spawn kills it.
"""
import json
import os
import socket
import subprocess
import sys
import threading
from dataclasses import replace

import pytest

from tests.gateway.test_session_kanban_capsule_guard import _policy

pytestmark = pytest.mark.platforms('linux')

# The child reads the inherited fd named by the owner-only argv flag and echoes the start line it receives.
# run22: the owner's observer line arrives first on that fd; the child prints start, then the observer line.
CHILD = ("import socket,sys;a=sys.argv[1:];assert a[0]=='--observation-fd',a;"
         "s=socket.socket(fileno=int(a[1]));f=s.makefile('rb');o=f.readline();"
         "print(f.readline().decode().strip(),flush=True);print(o.decode().strip(),flush=True)")
# Reads the first line with SO_PASSCRED and reports the SCM_CREDENTIALS sender of every byte of it.
CHILD_CREDS = ("import json,socket,struct,sys;a=sys.argv[1:];s=socket.socket(fileno=int(a[1]));"
               "s.setsockopt(socket.SOL_SOCKET,socket.SO_PASSCRED,1);buf=b'';pids=set()\n"
               "while not buf.endswith(b'\\n'):\n"
               " d,anc,fl,_=s.recvmsg(1,socket.CMSG_SPACE(12))\n"
               " assert d\n"
               " buf+=d;pids|={struct.unpack('3i',v)[0] for l,t,v in anc if t==socket.SCM_CREDENTIALS}\n"
               "print(json.dumps({'line':json.loads(buf),'senders':sorted(pids)}),flush=True)")


TEST_DISPATCH_OWNER = 'test-dispatcher'
_UNSET = object()


def board_dispatch_owner(db):
    """The board's persisted dispatch owner; the test board gets one (like a provisioned board) if unset."""
    from hermes_cli.kanban_dispatch_owner import get_dispatch_owner, set_dispatch_owner
    return get_dispatch_owner(db) or set_dispatch_owner(db, TEST_DISPATCH_OWNER)


_OBSERVER_ROOT = []


def observer_fields():
    """run22: the trusted observer root + digest the dispatcher host registers (a staged private dir)."""
    import tempfile
    from pathlib import Path
    from tests.agent.test_managed_worker_observer_load import root_digest, stage_observer
    if not _OBSERVER_ROOT:
        _OBSERVER_ROOT.append(stage_observer(Path(tempfile.mkdtemp(prefix='observer-root-'))))
    root = _OBSERVER_ROOT[0]
    return {'observer_root': str(root), 'observer_digest': root_digest(root)}


def registration_for(db, **override):
    """Acceptor registration content (``D/acceptor.json``) for THIS process as the listener."""
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    from pathlib import Path
    real = str(Path(db).resolve())
    body = {'v': 1, 'db': real, 'dispatch_owner': board_dispatch_owner(real), 'pid': os.getpid(),
            'start': _process_fingerprint(os.getpid()), **observer_fields()}
    body.update(override)
    return body


def write_registration(folder, body, *, mode=0o600):
    """Atomic tmp + rename inside the rendezvous dir (what the dispatcher acceptor does at startup)."""
    data = body if isinstance(body, bytes) else json.dumps(body, sort_keys=True).encode()
    tmp = folder / '.acceptor.json.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, folder / 'acceptor.json')


class Acceptor:
    """Dispatcher-side stand-in: listens at the deterministic run socket and speaks the documented wire.

    It registers itself in ``D/acceptor.json`` (bound to the board's dispatch owner and this process's
    pid + start fingerprint) and echoes ``dispatch_owner`` in the hello reply, as the decided spec requires.
    """

    def __init__(self, context, *, echo=None, accept_fd=True, mode=0o700, register=True, registration=None,
                 reply_owner=_UNSET):
        from gateway.session_kanban_rendezvous import rendezvous_dir
        self.context = context
        self.dir = rendezvous_dir(context['db'])
        self.dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.dir, mode)
        self.path = self.dir / f"{context['run_id']}.sock"
        self.echo, self.accept_fd = echo or {}, accept_fd
        self.hellos, self.payloads, self.fds = [], [], []
        self.registration = registration_for(context['db']) if registration is None else registration
        if register:
            write_registration(self.dir, self.registration)
        self.reply_owner = (self.registration.get('dispatch_owner') if isinstance(self.registration, dict)
                            else None) if reply_owner is _UNSET else reply_owner
        dfd = os.open(self.dir, os.O_RDONLY | os.O_DIRECTORY)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.server.bind(f'/proc/self/fd/{dfd}/{self.path.name}')
        finally:
            os.close(dfd)
        self.server.listen(8)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def identity(self):
        from pathlib import Path
        c = self.context
        return {'db': str(Path(c['db']).resolve()), 'task_id': c['task_id'], 'run_id': c['run_id'],
                'claim_lock': c['claim_lock']}

    def _serve(self):
        self.server.settimeout(0.2)
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                try:
                    self._one(conn)
                except (OSError, ValueError):
                    pass

    def _one(self, conn):
        conn.settimeout(5)
        reader = conn.makefile('rb')
        hello = json.loads(reader.readline())
        self.hellos.append(hello)
        reply = {'v': 1, 'ok': True, **self.identity(), **self.echo}
        if self.reply_owner is not _UNSET and self.reply_owner is not None:
            reply['dispatch_owner'] = self.reply_owner
        conn.sendall(json.dumps(reply).encode() + b'\n')
        if hello.get('op') != 'attach':
            return
        msg, fds, _flags, _addr = socket.recv_fds(conn, 65536, 1)
        payload = json.loads(msg.split(b'\n', 1)[0])
        self.payloads.append(payload)
        if not self.accept_fd or len(fds) != 1:
            conn.sendall(b'{"v":1,"ok":false,"reason":"refused"}\n')
            for fd in fds:
                os.close(fd)
            return
        channel = socket.socket(fileno=fds[0])
        self.fds.append(channel)
        channel.sendall(json.dumps({'v': 1, 'op': 'start', 'task_id': payload['task_id'], 'run_id': payload['run_id'],
                                    'claim_lock': payload['claim_lock']}).encode() + b'\n')
        conn.sendall(b'{"v":1,"ok":true,"received":true}\n')

    def close(self):
        self.stop.set()
        self.thread.join(5)
        self.server.close()
        for channel in self.fds:
            channel.close()


@pytest.fixture
def required(tmp_path, monkeypatch):
    policy = _policy(tmp_path, monkeypatch, local=False)
    assert json.loads(policy.kanban_json)['capsule']['required'] is True
    monkeypatch.setattr('gateway.session_kanban_scope.resolve_run_scope', lambda p: None)
    # Exercise the production prelaunch check against an actually clean boot tree.
    from hermes_cli import git_fingerprint
    core = tmp_path / 'owner-core'
    core.mkdir()
    subprocess.run(['git', 'init', '-q', str(core)], check=True)
    (core / 'owner.py').write_text('VALUE = 1\n')
    subprocess.run(['git', '-C', str(core), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(core), '-c', 'user.name=Fixture',
                    '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'owner'], check=True)
    git_fingerprint.record_process_boot_fingerprint(core)
    git_fingerprint.record_capsule_boot_identity(core)
    monkeypatch.setattr('gateway.capsule_contract.CORE_ROOT', core)
    # This fixture proves rendezvous, not native owner bootstrap provenance.
    # Keep the real disk check; scope the installed-ledger boundary explicitly.
    monkeypatch.setattr('hermes_cli.code_attestation.integrity_observation', lambda: {
        'core_root': str(core), 'boot_sha': git_fingerprint.capsule_code_sha(core),
        'route': 'managed-stub', 'unknowns': ()})
    return policy


@pytest.fixture
def acceptors():
    made = []
    yield made
    for acceptor in made:
        acceptor.close()


def _acceptor(acceptors, policy, **kw):
    acceptor = Acceptor(json.loads(policy.kanban_json), **kw)
    acceptors.append(acceptor)
    return acceptor


class Recorder:
    def __init__(self):
        self.calls, self.processes = [], []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        # Identity of each inherited end at spawn time: fd numbers are reused once the owner closes them.
        self.passed = {fd: _socket_link(fd) for fd in kwargs.get('pass_fds', ())}
        process = subprocess.Popen(*args, **kwargs)
        self.processes.append(process)
        return process


def _spawn(policy):
    from gateway.session_kanban_scope import placing_spawn
    recorder = Recorder()
    run = placing_spawn(recorder, policy)
    return recorder, run


def _launch(run):
    return run([sys.executable, '-c', CHILD], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
               start_new_session=True, close_fds=True)


def _refused(excinfo, reason=None):
    assert str(excinfo.value) == 'capsule_observation_missing'
    if reason is not None:
        assert reason in str(excinfo.value.__cause__)


def _gone(process):
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return False
    return process.returncode is not None


def test_owner_prehello_error_records_loss_and_consumed_rendezvous(required, acceptors, monkeypatch):
    """Real owner execute + rendezvous; tiny child supplies the native refusal frame.

    Native unmodified child refusal is independently covered in its subprocess suite.
    A separate real submitter stand-in exits after loss; native dispatch then books
    the attempt crashed. This is not a full submitter/private-observer E2E.
    """
    import asyncio
    from types import SimpleNamespace
    from gateway import session_managed_worker as managed
    from gateway.session_contract import SessionRef
    from hermes_state_runtime import RuntimeStoreError
    from hermes_cli.kanban_db_connect import connect_closing
    acceptor = _acceptor(acceptors, required)
    context = json.loads(required.kanban_json)
    original = subprocess.Popen
    spawned = []
    class RefusingChild(original):
        def __init__(self, argv, **kwargs):
            managed_child = len(argv) > 3 and argv[1:3] == ['-I', '-c']
            if managed_child:
                argv = [sys.executable, '-c',
                    'import sys;print(\'{"type":"error","reason":"managed_worker_failed"}\',flush=True);sys.stdin.read()']
            super().__init__(argv, **kwargs)
            if managed_child:
                spawned.append(self)
    monkeypatch.setattr(managed.subprocess, 'Popen', RefusingChild)
    monkeypatch.setattr(managed, '_worker_env', lambda authority: None)
    monkeypatch.setattr('gateway.session_managed_worker_log.worker_log_path', lambda profile: None)
    authority = SimpleNamespace(profile_id='fixture', pending_results={}, waiters={},
        adopt_agent=lambda *args: None, _require_admission_open=lambda: None)
    row = {'admission_id': 'adm', 'principal_id': 'owner', 'generation': 1, 'payload': {'text': 'go'}}
    async def scenario():
        with pytest.raises(RuntimeStoreError):
            await managed.execute_managed(authority, SessionRef('fixture', 's'), row, required)
        tasks = list(getattr(authority, '_mutation_tasks', ()))
        if tasks:
            await asyncio.gather(*tasks)
    # A real stand-in submitter has its own native spawn receipt; the claim
    # fixture alone has no submitted process, so no death can be inferred from it.
    from hermes_cli import kanban_db_dispatch as dispatch
    from pathlib import Path
    submitter = original(['/usr/bin/cat'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    try:
        with connect_closing(db_path=Path(context['db'])) as conn:
            dispatch._set_worker_pid(conn, context['task_id'], submitter.pid,
                                     run_id=context['run_id'], claim_lock=context['claim_lock'])
        asyncio.run(scenario())
    finally:
        submitter.stdin.close()
        submitter.wait(timeout=5)
    assert authority._managed_workers == {} and authority._managed_spawning == {}
    assert authority.pending_results == {}  # no successful/interrupt outcome fabricated
    assert len(spawned) == 1 and spawned[0].poll() is not None
    assert spawned[0].stdin.closed and spawned[0].stdout.closed
    assert (acceptor.dir / f"{context['run_id']}.consumed").exists()
    from gateway.session_kanban_capsule import _live_transport_ready
    assert _live_transport_ready(context) is False  # consumed once, never reusable
    with connect_closing(db_path=Path(context['db'])) as conn:
        events = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='worker_lost'",
                              (context['task_id'],)).fetchall()
    assert len(events) == 1
    assert json.loads(events[0][0])['bound'] is False
    # Replay the real dispatcher, not a hand-written terminal row. A prehello
    # refusal is a failed attempt and must never be mistaken for successful work.
    monkeypatch.setenv('HERMES_KANBAN_CRASH_GRACE_SECONDS', '0')
    with connect_closing(db_path=Path(context['db'])) as conn:
        result = dispatch.dispatch_once(conn, max_spawn=0, stale_timeout_seconds=0, failure_limit=2,
                                        dispatcher_id=TEST_DISPATCH_OWNER)
        assert not result.skipped_owned, result
        run = conn.execute('SELECT status,outcome,error FROM task_runs WHERE id=?',
                           (context['run_id'],)).fetchone()
        task = conn.execute('SELECT status,current_run_id,consecutive_failures FROM tasks WHERE id=?',
                            (context['task_id'],)).fetchone()
    assert run['status'] == 'crashed' and run['outcome'] == 'crashed'
    assert 'managed worker lost:' in run['error']
    assert task['status'] == 'ready' and task['current_run_id'] is None
    assert task['consecutive_failures'] == 1


def test_authenticated_listener_receives_fd_and_child_gets_start(required, acceptors):
    acceptor = _acceptor(acceptors, required)
    recorder, run = _spawn(required)
    process = _launch(run)
    try:
        out = process.stdout.readline().decode()
        start = json.loads(out)
        assert start['op'] == 'start'
    finally:
        process.wait(timeout=10)
        process.stdout.close()
        process.stdin.close()
    context = json.loads(required.kanban_json)
    [payload] = acceptor.payloads
    assert payload['task_id'] == context['task_id'] and payload['run_id'] == context['run_id']
    assert payload['claim_lock'] == context['claim_lock']
    assert payload['child_pid'] == process.pid and payload['owner_pid'] == os.getpid()
    assert isinstance(payload['child_start'], str) and payload['child_start']
    assert acceptor.hellos[-1]['op'] == 'attach'
    assert acceptor.hellos[-1]['owner_pid'] == os.getpid()
    [(args, kwargs)] = recorder.calls
    from gateway.capsule_contract import CORE_ROOT, WORKER_FLAG
    from hermes_cli.git_fingerprint import capsule_code_sha
    assert args[0][:2] == [sys.executable, '-c']
    assert args[0][-4] == '--observation-fd'
    assert args[0][-2:] == [WORKER_FLAG, capsule_code_sha(CORE_ROOT)]
    assert kwargs['pass_fds'] == (int(args[0][-3]),)
    # The owner keeps no copy of either end once brokered. Compare socket identities, not fd numbers:
    # the freed child-end number is reused by the next allocation in this process (the acceptor's
    # recv_fds, any other thread), which made a number-based check flaky.
    child_end = recorder.passed[int(args[0][-3])]
    [channel] = acceptor.fds
    parent_end = _socket_link(channel.fileno())
    links = _open_links()
    assert child_end not in links.values()
    assert [fd for fd, link in links.items() if link == parent_end] == [channel.fileno()]
    assert (acceptor.dir / f"{context['run_id']}.consumed").exists()


def _socket_link(fd):
    link = os.readlink(f'/proc/self/fd/{fd}')
    assert link.startswith('socket:['), link
    return link


def _open_links():
    links = {}
    for name in os.listdir('/proc/self/fd'):
        try:
            links[int(name)] = os.readlink(f'/proc/self/fd/{name}')
        except OSError:
            pass  # the listdir fd itself, or one closed meanwhile
    return links


def test_fingerprint_matches_worker_bound_shape(required, acceptors):
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    acceptor = _acceptor(acceptors, required)
    _recorder, run = _spawn(required)
    process = _launch(run)
    try:
        process.stdout.readline()
        assert acceptor.payloads[0]['child_start'] == _process_fingerprint(process.pid)
    finally:
        process.wait(timeout=10)
        process.stdout.close()
        process.stdin.close()


def test_probe_is_ready_only_with_authenticated_listener_and_does_not_consume(required, acceptors):
    from gateway.session_kanban_capsule import require_capsule_transport, _live_transport_ready
    acceptor = _acceptor(acceptors, required)
    require_capsule_transport(required)  # admission-time probe: authenticated, not consumed
    assert _live_transport_ready(json.loads(required.kanban_json)) is True
    assert [h['op'] for h in acceptor.hellos] == ['probe', 'probe']
    assert not any(acceptor.dir.glob('*.consumed'))


def test_socket_dir_not_0700_refuses(required, acceptors):
    from gateway.session_kanban_capsule import require_capsule_transport
    from hermes_state_runtime import RuntimeStoreError
    _acceptor(acceptors, required, mode=0o750)
    with pytest.raises(RuntimeStoreError) as excinfo:
        require_capsule_transport(required)
    _refused(excinfo)
    recorder, run = _spawn(required)
    with pytest.raises(RuntimeStoreError) as excinfo:
        _launch(run)
    _refused(excinfo, 'capsule_rendezvous_dir_unsafe')
    assert recorder.calls == []


def test_symlinked_socket_dir_refuses(required, acceptors, tmp_path):
    from hermes_state_runtime import RuntimeStoreError
    from gateway.session_kanban_rendezvous import rendezvous_dir
    context = json.loads(required.kanban_json)
    real = tmp_path / 'elsewhere'
    real.mkdir(mode=0o700)
    link = rendezvous_dir(context['db'])
    link.symlink_to(real, target_is_directory=True)
    acceptor = _acceptor(acceptors, required)  # binds through the symlink into the real dir
    assert acceptor.path.is_socket()
    recorder, run = _spawn(required)
    with pytest.raises(RuntimeStoreError) as excinfo:
        _launch(run)
    _refused(excinfo, 'capsule_rendezvous_dir_unsafe')
    assert recorder.calls == [] and acceptor.hellos == []


def test_wrong_listener_uid_refuses(required, acceptors, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    from gateway import session_kanban_rendezvous as rdv
    acceptor = _acceptor(acceptors, required)
    real = rdv._peer_cred
    monkeypatch.setattr(rdv, '_peer_cred', lambda sock: (real(sock)[0], os.getuid() + 1, real(sock)[2]))
    recorder, run = _spawn(required)
    with pytest.raises(RuntimeStoreError) as excinfo:
        _launch(run)
    _refused(excinfo, 'capsule_rendezvous_peer_uid')
    assert recorder.calls == [] and acceptor.hellos == []


def test_dir_or_socket_not_owned_by_owner_refuses(required, acceptors, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    from gateway import session_kanban_rendezvous as rdv
    _acceptor(acceptors, required)
    monkeypatch.setattr(rdv, '_owner_uid', lambda: os.getuid() + 1)
    recorder, run = _spawn(required)
    with pytest.raises(RuntimeStoreError) as excinfo:
        _launch(run)
    _refused(excinfo, 'capsule_rendezvous_dir_unsafe')
    assert recorder.calls == []


def test_non_socket_at_run_path_refuses(required):
    from hermes_state_runtime import RuntimeStoreError
    from gateway.session_kanban_rendezvous import rendezvous_dir
    context = json.loads(required.kanban_json)
    folder = rendezvous_dir(context['db'])
    folder.mkdir(mode=0o700)
    (folder / f"{context['run_id']}.sock").write_text('x')
    recorder, run = _spawn(required)
    with pytest.raises(RuntimeStoreError) as excinfo:
        _launch(run)
    _refused(excinfo, 'capsule_rendezvous_socket_unsafe')
    assert recorder.calls == []


@pytest.mark.parametrize('field', ['run_id', 'claim_lock', 'task_id', 'db'])
def test_identity_mismatch_at_hello_refuses_before_spawn(required, acceptors, field):
    from gateway.session_kanban_capsule import _live_transport_ready
    from hermes_state_runtime import RuntimeStoreError
    wrong = {'run_id': 999, 'claim_lock': 'other:1', 'task_id': 't_other', 'db': '/elsewhere/kanban.db'}[field]
    _acceptor(acceptors, required, echo={field: wrong})
    assert _live_transport_ready(json.loads(required.kanban_json)) is False
    recorder, run = _spawn(required)
    with pytest.raises(RuntimeStoreError) as excinfo:
        _launch(run)
    _refused(excinfo, 'capsule_rendezvous_identity')
    assert recorder.calls == []


def test_listener_refusing_fd_kills_child(required, acceptors):
    from hermes_state_runtime import RuntimeStoreError
    _acceptor(acceptors, required, accept_fd=False)
    recorder, run = _spawn(required)
    with pytest.raises(RuntimeStoreError) as excinfo:
        _launch(run)
    _refused(excinfo, 'capsule_rendezvous_refused')
    [process] = recorder.processes
    assert _gone(process)


def test_delivery_failure_after_spawn_kills_child(required, acceptors, monkeypatch):
    """A broken listener between hello and FD (identity drift on the board) still kills the child."""
    from hermes_state_runtime import RuntimeStoreError
    from gateway import session_kanban_rendezvous as rdv
    _acceptor(acceptors, required)
    monkeypatch.setattr(rdv, '_child_fingerprint', lambda pid: None)
    recorder, run = _spawn(required)
    with pytest.raises(RuntimeStoreError) as excinfo:
        _launch(run)
    _refused(excinfo, 'capsule_rendezvous_child_unverified')
    [process] = recorder.processes
    assert _gone(process)


def test_no_rendezvous_required_card_refuses_and_no_child_survives(required):
    from hermes_state_runtime import RuntimeStoreError
    recorder, run = _spawn(required)
    with pytest.raises(RuntimeStoreError) as excinfo:
        _launch(run)
    _refused(excinfo)
    assert recorder.calls == [] and recorder.processes == []


def test_no_rendezvous_ordinary_card_spawn_is_byte_identical(tmp_path, monkeypatch):
    from gateway.session_kanban_scope import placing_spawn
    policy = _policy(tmp_path, monkeypatch, local=True)
    monkeypatch.setattr('gateway.session_kanban_scope.resolve_run_scope', lambda p: None)
    seen = []
    argv = [sys.executable, '-m', 'agent.managed_worker']
    kwargs = dict(cwd=str(tmp_path), stdin=subprocess.PIPE, env={'A': '1'}, stdout=subprocess.PIPE,
                  stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    placing_spawn(lambda *a, **k: seen.append((a, k)) or 'p', policy)(list(argv), **dict(kwargs))
    seen.append(((list(argv),), dict(kwargs)))
    assert seen[0] == seen[1]
    assert 'pass_fds' not in seen[0][1] and seen[0][0] == (argv,)


def test_ordinary_card_ignores_a_present_rendezvous(tmp_path, monkeypatch, acceptors):
    from gateway.session_kanban_scope import placing_spawn
    policy = _policy(tmp_path, monkeypatch, local=True)
    monkeypatch.setattr('gateway.session_kanban_scope.resolve_run_scope', lambda p: None)
    acceptor = _acceptor(acceptors, policy)
    seen = []
    placing_spawn(lambda *a, **k: seen.append((a, k)) or 'p', policy)(['x'], close_fds=True)
    assert seen == [((['x'],), {'close_fds': True})] and acceptor.hellos == []


def test_second_spawn_for_same_run_is_refused(required, acceptors):
    from gateway.session_kanban_capsule import _live_transport_ready
    from hermes_state_runtime import RuntimeStoreError
    acceptor = _acceptor(acceptors, required)
    _recorder, run = _spawn(required)
    process = _launch(run)
    process.stdout.readline()
    process.wait(timeout=10)
    process.stdout.close()
    process.stdin.close()
    again, run2 = _spawn(required)
    with pytest.raises(RuntimeStoreError) as excinfo:
        _launch(run2)
    _refused(excinfo, 'capsule_rendezvous_consumed')
    assert again.calls == []
    assert _live_transport_ready(json.loads(required.kanban_json)) is False
    assert len(acceptor.payloads) == 1


def test_replayed_policy_for_another_run_does_not_reach_this_socket(required, acceptors):
    """A policy naming a different run looks for that run's socket, never this one's."""
    from hermes_state_runtime import RuntimeStoreError
    acceptor = _acceptor(acceptors, required)
    context = json.loads(required.kanban_json)
    other = replace(required, kanban_json=json.dumps({**context, 'run_id': context['run_id'] + 1}))
    recorder, run = _spawn(other)
    with pytest.raises(RuntimeStoreError):
        _launch(run)
    assert recorder.calls == [] and acceptor.hellos == []


@pytest.mark.parametrize('env', ['HERMES_CAPSULE_TRANSPORT_READY', 'HERMES_KANBAN_CAPSULE_READY',
                                 'HERMES_CAPSULE_RENDEZVOUS', 'HERMES_OBSERVATION_FD'])
def test_env_or_config_cannot_force_ready(required, monkeypatch, tmp_path, env):
    import inspect
    from gateway.session_kanban_capsule import _live_transport_ready, require_capsule_transport
    from hermes_state_runtime import RuntimeStoreError
    monkeypatch.setenv(env, '1')
    with open(tmp_path / 'config.yaml', 'a', encoding='utf-8'):
        pass
    cfg = json.loads((tmp_path / 'config.yaml').read_text())
    cfg['kanban'] = {'capsule_transport_ready': True, 'capsule': {'ready': True}}
    (tmp_path / 'config.yaml').write_text(json.dumps(cfg))
    assert list(inspect.signature(_live_transport_ready).parameters) == ['context']
    assert _live_transport_ready(json.loads(required.kanban_json)) is False
    with pytest.raises(RuntimeStoreError, match='capsule_observation_missing'):
        require_capsule_transport(required)
    recorder, run = _spawn(required)
    with pytest.raises(RuntimeStoreError, match='capsule_observation_missing'):
        _launch(run)
    assert recorder.calls == []


def test_rendezvous_dir_is_derived_from_board_db(required):
    from pathlib import Path
    from gateway.session_kanban_rendezvous import rendezvous_dir
    db = json.loads(required.kanban_json)['db']
    assert rendezvous_dir(db) == Path(db).resolve().parent / 'capsule-rendezvous'


# ---- run20: the listener must be the board's registered dispatch owner (D/acceptor.json) ----

def _refused_before_hello(policy, acceptor, code):
    """probe not ready, launch refused with ``code``, nothing spawned, no hello ever reached the listener."""
    from gateway.session_kanban_capsule import _live_transport_ready
    from hermes_state_runtime import RuntimeStoreError
    assert _live_transport_ready(json.loads(policy.kanban_json)) is False
    recorder, run = _spawn(policy)
    with pytest.raises(RuntimeStoreError) as excinfo:
        _launch(run)
    _refused(excinfo, code)
    assert recorder.calls == [] and recorder.processes == []
    assert acceptor.hellos == []
    assert not any(acceptor.dir.glob('*.consumed'))


def test_registered_dispatch_owner_listener_is_accepted(required, acceptors):
    from gateway.session_kanban_capsule import _live_transport_ready
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    acceptor = _acceptor(acceptors, required)
    folder_mode = os.stat(acceptor.dir / 'acceptor.json').st_mode & 0o777
    registration = json.loads((acceptor.dir / 'acceptor.json').read_text())
    assert folder_mode == 0o600
    assert registration == {'v': 1, 'db': acceptor.identity()['db'], 'dispatch_owner': TEST_DISPATCH_OWNER,
                            'pid': os.getpid(), 'start': _process_fingerprint(os.getpid()), **observer_fields()}
    assert _live_transport_ready(json.loads(required.kanban_json)) is True
    _recorder, run = _spawn(required)
    process = _launch(run)
    try:
        assert json.loads(process.stdout.readline())['op'] == 'start'
    finally:
        process.wait(timeout=10)
        process.stdout.close()
        process.stdin.close()
    # admission probe, then the non-consuming launch preflight (run27 B-m1: before scope resolution), then one attach
    assert [h['op'] for h in acceptor.hellos] == ['probe', 'probe', 'attach'] and len(acceptor.payloads) == 1


def test_unregistered_listener_refuses(required, acceptors):
    acceptor = _acceptor(acceptors, required, register=False)
    _refused_before_hello(required, acceptor, 'capsule_acceptor_unregistered')


def _bad_registration(kind, tmp_path, db):
    good = registration_for(db)
    return {
        'not_json': b'not json',
        'not_object': b'[]',
        'missing_start': {k: v for k, v in good.items() if k != 'start'},
        'extra_key': dict(good, token='x'),
        'wrong_version': dict(good, v=2),
        'pid_not_int': dict(good, pid=str(good['pid'])),
        'pid_bool': dict(good, pid=True),
        'start_not_str': dict(good, start=123),
        'other_db': dict(good, db=str(tmp_path / 'elsewhere' / 'kanban.db')),
        'relative_db': dict(good, db='kanban.db'),
        'oversize': json.dumps(good).encode() + b' ' * 5000,
    }[kind]


@pytest.mark.parametrize('kind', ['not_json', 'not_object', 'missing_start', 'extra_key', 'wrong_version',
                                  'pid_not_int', 'pid_bool', 'start_not_str', 'other_db', 'relative_db',
                                  'oversize'])
def test_malformed_or_foreign_registration_refuses(required, acceptors, tmp_path, kind):
    context = json.loads(required.kanban_json)
    acceptor = _acceptor(acceptors, required, registration=_bad_registration(kind, tmp_path, context['db']))
    _refused_before_hello(required, acceptor, 'capsule_acceptor_unregistered')


@pytest.mark.parametrize('kind', ['mode_0644', 'mode_0400', 'symlink', 'directory', 'fifo'])
def test_unsafe_registration_file_refuses(required, acceptors, tmp_path, kind):
    acceptor = _acceptor(acceptors, required)
    target = acceptor.dir / 'acceptor.json'
    if kind.startswith('mode_'):
        os.chmod(target, int(kind[5:], 8))
    else:
        real = tmp_path / 'real-registration.json'
        real.write_bytes(target.read_bytes())
        os.chmod(real, 0o600)
        target.unlink()
        if kind == 'symlink':
            target.symlink_to(real)
        elif kind == 'directory':
            target.mkdir(mode=0o700)
        else:
            os.mkfifo(target, 0o600)
    _refused_before_hello(required, acceptor, 'capsule_acceptor_unregistered')


def test_registration_not_owned_by_owner_uid_refuses(required, acceptors, monkeypatch):
    """Only the registration file's owner differs (dir + socket stay owned by the owner uid)."""
    from gateway import session_kanban_rendezvous as rdv
    acceptor = _acceptor(acceptors, required)
    real_fstat = os.fstat
    reg_ino = os.stat(acceptor.dir / 'acceptor.json').st_ino

    def fstat(fd):
        st = real_fstat(fd)
        if st.st_ino != reg_ino:
            return st
        fields = list(st)
        fields[4] = os.getuid() + 1
        return os.stat_result(fields)
    monkeypatch.setattr(rdv.os, 'fstat', fstat)
    _refused_before_hello(required, acceptor, 'capsule_acceptor_unregistered')


@pytest.mark.parametrize('owner', ['someone-else', '', None, 7])
def test_registration_for_another_dispatch_owner_refuses(required, acceptors, owner):
    context = json.loads(required.kanban_json)
    acceptor = _acceptor(acceptors, required, registration=registration_for(context['db'], dispatch_owner=owner))
    _refused_before_hello(required, acceptor, 'capsule_acceptor_owner_mismatch')


def test_board_without_dispatch_owner_refuses(required, acceptors):
    from hermes_cli.kanban_dispatch_owner import set_dispatch_owner
    acceptor = _acceptor(acceptors, required)
    set_dispatch_owner(acceptor.identity()['db'], None)
    _refused_before_hello(required, acceptor, 'capsule_acceptor_owner_mismatch')


def test_board_owner_changed_after_registration_refuses(required, acceptors):
    from hermes_cli.kanban_dispatch_owner import set_dispatch_owner
    acceptor = _acceptor(acceptors, required)
    set_dispatch_owner(acceptor.identity()['db'], 'new-dispatcher')
    _refused_before_hello(required, acceptor, 'capsule_acceptor_owner_mismatch')


def test_listener_pid_not_the_registered_pid_refuses(required, acceptors):
    """Another live same-uid process is registered; this (squatting) listener is not it."""
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        context = json.loads(required.kanban_json)
        reg = registration_for(context['db'], pid=other.pid, start=_process_fingerprint(other.pid))
        acceptor = _acceptor(acceptors, required, registration=reg)
        _refused_before_hello(required, acceptor, 'capsule_acceptor_pid_mismatch')
    finally:
        other.kill()
        other.wait(timeout=10)


def test_registration_of_a_dead_process_refuses(required, acceptors):
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    gone = subprocess.Popen([sys.executable, '-c', 'pass'])
    start = _process_fingerprint(gone.pid) or '0|0'
    gone.wait(timeout=10)
    context = json.loads(required.kanban_json)
    acceptor = _acceptor(acceptors, required, registration=registration_for(context['db'], pid=gone.pid, start=start))
    _refused_before_hello(required, acceptor, 'capsule_acceptor_pid_mismatch')


def test_stale_registration_start_refuses(required, acceptors):
    """Same pid, different start fingerprint: a recycled pid or a registration left by an earlier process."""
    context = json.loads(required.kanban_json)
    acceptor = _acceptor(acceptors, required, registration=registration_for(context['db'], start='0|0'))
    _refused_before_hello(required, acceptor, 'capsule_acceptor_stale')


def test_unverifiable_listener_fingerprint_refuses(required, acceptors, monkeypatch):
    from gateway import session_kanban_rendezvous as rdv
    context = json.loads(required.kanban_json)
    acceptor = _acceptor(acceptors, required, registration=registration_for(context['db'], start='unverified'))
    monkeypatch.setattr(rdv, '_listener_fingerprint', lambda pid: 'unverified')
    _refused_before_hello(required, acceptor, 'capsule_acceptor_stale')


@pytest.mark.parametrize('reply_owner', [None, 'someone-else', ''])
def test_hello_reply_must_echo_registered_dispatch_owner(required, acceptors, reply_owner):
    from gateway.session_kanban_capsule import _live_transport_ready
    from hermes_state_runtime import RuntimeStoreError
    _acceptor(acceptors, required, reply_owner=reply_owner)
    assert _live_transport_ready(json.loads(required.kanban_json)) is False
    recorder, run = _spawn(required)
    with pytest.raises(RuntimeStoreError) as excinfo:
        _launch(run)
    _refused(excinfo, 'capsule_rendezvous_identity')
    assert recorder.calls == []


def test_hello_reply_with_extra_keys_refuses(required, acceptors):
    from gateway.session_kanban_capsule import _live_transport_ready
    _acceptor(acceptors, required, echo={'grant': True})
    assert _live_transport_ready(json.loads(required.kanban_json)) is False


# ---- run22: the owner pins the trusted observer from the verified registration ----

def _launch_with(run, code):
    return run([sys.executable, '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
               start_new_session=True, close_fds=True)


def test_owner_writes_observer_line_from_registration_before_fd_handoff(required, acceptors):
    acceptor = _acceptor(acceptors, required)
    _recorder, run = _spawn(required)
    process = _launch(run)
    try:
        start = json.loads(process.stdout.readline())
        observer = json.loads(process.stdout.readline())
    finally:
        process.wait(timeout=10)
        process.stdout.close()
        process.stdin.close()
    assert start['op'] == 'start'  # the observer line was already in the child's end before the fd moved
    reg = acceptor.registration
    assert observer == {'v': 1, 'op': 'observer', 'root': reg['observer_root'], 'digest': reg['observer_digest'],
                        'host_pid': reg['pid']}
    assert len(acceptor.payloads) == 1


def test_observer_line_sender_is_the_owner(required, acceptors):
    _acceptor(acceptors, required)
    _recorder, run = _spawn(required)
    process = _launch_with(run, CHILD_CREDS)
    try:
        out = json.loads(process.stdout.readline())
    finally:
        process.wait(timeout=10)
        process.stdout.close()
        process.stdin.close()
    assert out['line']['op'] == 'observer' and out['senders'] == [os.getpid()]


@pytest.mark.parametrize('drop', [('observer_root', 'observer_digest'), ('observer_root',), ('observer_digest',)])
def test_registration_without_observer_fields_refuses_before_spawn(required, acceptors, drop):
    context = json.loads(required.kanban_json)
    reg = {k: v for k, v in registration_for(context['db']).items() if k not in drop}
    acceptor = _acceptor(acceptors, required, registration=reg)
    _refused_before_hello(required, acceptor, 'capsule_observer_untrusted')


def _bad_observer(kind, tmp_path):
    fields = observer_fields()
    root = fields['observer_root']
    link = tmp_path / 'observer-link'
    if kind == 'symlink_root':
        link.symlink_to(root, target_is_directory=True)
    return {
        'relative_root': dict(fields, observer_root='observer-root'),
        'root_not_str': dict(fields, observer_root=7),
        'root_empty': dict(fields, observer_root=''),
        'root_not_canonical': dict(fields, observer_root=root + '/../' + os.path.basename(root)),
        'root_trailing_slash': dict(fields, observer_root=root + '/'),
        'symlink_root': dict(fields, observer_root=str(link)),
        'digest_short': dict(fields, observer_digest='ab' * 16),
        'digest_upper': dict(fields, observer_digest=fields['observer_digest'].upper()),
        'digest_not_hex': dict(fields, observer_digest='z' * 64),
        'digest_not_str': dict(fields, observer_digest=None),
    }[kind]


@pytest.mark.parametrize('kind', ['relative_root', 'root_not_str', 'root_empty', 'root_not_canonical',
                                  'root_trailing_slash', 'symlink_root', 'digest_short', 'digest_upper',
                                  'digest_not_hex', 'digest_not_str'])
def test_malformed_observer_fields_refuse_before_spawn(required, acceptors, tmp_path, kind):
    context = json.loads(required.kanban_json)
    reg = registration_for(context['db'], **_bad_observer(kind, tmp_path))
    acceptor = _acceptor(acceptors, required, registration=reg)
    _refused_before_hello(required, acceptor, 'capsule_observer_untrusted')


def test_scope_resolution_failure_does_not_consume_rendezvous(required, acceptors, monkeypatch):
    """[daveey] run27 B-m1: the run scope is resolved BEFORE the single-use rendezvous is consumed, so a
    scope failure refuses the launch without burning the run; a later launch of the same run succeeds."""
    from gateway.session_kanban_scope import ScopePlacementError

    def unready(policy):
        raise ScopePlacementError('kanban_scope_missing')
    monkeypatch.setattr('gateway.session_kanban_scope.resolve_run_scope', unready)
    acceptor = _acceptor(acceptors, required)
    context = json.loads(required.kanban_json)
    recorder, run = _spawn(required)
    with pytest.raises(ScopePlacementError):
        _launch(run)
    assert recorder.calls == []
    assert [h['op'] for h in acceptor.hellos if h.get('op') == 'attach'] == []
    assert not (acceptor.dir / f"{context['run_id']}.consumed").exists()
    monkeypatch.setattr('gateway.session_kanban_scope.resolve_run_scope', lambda p: None)
    recorder, run = _spawn(required)
    process = _launch(run)
    try:
        assert json.loads(process.stdout.readline().decode())['op'] == 'start'
    finally:
        process.wait(timeout=10)
        process.stdout.close()
        process.stdin.close()
    assert len(acceptor.payloads) == 1 and len(recorder.calls) == 1
    assert (acceptor.dir / f"{context['run_id']}.consumed").exists()
