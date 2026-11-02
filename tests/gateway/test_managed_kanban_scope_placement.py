"""A managed Kanban worker and its descendants land in the run's real, finite systemd scope."""
import asyncio
import errno
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import session_kanban_scope as scope_mod
from tests.waiting import async_wait_until

CHILD = ("import subprocess,sys;sys.stdin.readline();"
         "d=subprocess.Popen([sys.executable,'-c','import sys;sys.stdin.read()']);"
         "print(d.pid,flush=True);sys.stdin.read();d.wait()")


def _cgroup(pid):
    return scope_mod._cgroup_path(pid)


@pytest.fixture
def run_scope(tmp_path):
    """A disposable, memory-capped per-run scope holding a sleeping 'submitter', plus a tiny board."""
    if not (sys.platform.startswith('linux') and shutil.which('systemd-run')
            and scope_mod.CGROUP_ROOT.joinpath('cgroup.controllers').exists()):
        pytest.skip('needs Linux cgroup v2 + systemd-run')
    task_id, run_id, claim = f't_scopetest{uuid.uuid4().hex[:8]}', 7, 'host:1'
    unit = f'hermes-worker-kanban-{task_id}-run-{run_id}.scope'
    runtime = f'/run/user/{os.getuid()}'
    env = dict(os.environ, XDG_RUNTIME_DIR=runtime, DBUS_SESSION_BUS_ADDRESS=f'unix:path={runtime}/bus')
    submitter = subprocess.Popen(['systemd-run', '--user', '--scope', '--quiet', '--collect', '-p', 'MemoryMax=64M',
                                  '-u', unit, 'sleep', '120'], stderr=subprocess.PIPE, env=env,
                                 text=True)
    deadline = time.time() + 10
    while time.time() < deadline and not (_cgroup(submitter.pid) or '').endswith(unit):
        time.sleep(0.05)
    if not (_cgroup(submitter.pid) or '').endswith(unit):
        submitter.kill()
        submitter.wait()
        pytest.skip(f'cannot create a user scope here: {submitter.stderr.read()[:200]!r}')
    from hermes_cli.kanban_db_connect import connect_closing
    db = tmp_path / 'board.db'
    with connect_closing(db) as conn:
        conn.execute("INSERT INTO tasks (id,title,status,created_at,current_run_id,claim_lock) VALUES (?,?,?,?,?,?)",
                     (task_id, 't', 'running', 1, run_id, claim))
        conn.execute("INSERT INTO task_runs (id,task_id,status,claim_lock,worker_pid,started_at) VALUES (?,?,?,?,?,?)",
                     (run_id, task_id, 'running', claim, submitter.pid, 1))
        conn.commit()
    ctx = dict(task_id=task_id, run_id=run_id, claim_lock=claim, db=str(db))
    policy = SimpleNamespace(kanban_json=json.dumps(ctx))
    yield SimpleNamespace(policy=policy, unit=unit, db=db, ctx=ctx, submitter=submitter, tmp=tmp_path)
    submitter.kill()
    submitter.wait()


def _spawn(*args, **kwargs):
    return subprocess.Popen([sys.executable, '-c', CHILD], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            start_new_session=True, close_fds=True)


def _reap(process):
    if process.stdin and not process.stdin.closed:
        process.stdin.close()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        import signal
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    if process.stdout:
        process.stdout.close()


def _probe_migration(target):
    """Scope creation does not grant migration across a nondelegated common ancestor.

    Probe the kernel directly, not migrate(): a production placement regression must
    still fail on capable hosts. Only move our disposable child, never the test runner.
    """
    process = _spawn()
    try:
        path = target / 'cgroup.procs'
        try:
            with path.open('wb', buffering=0) as stream:
                stream.write(f'{process.pid}\n'.encode())
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
                raise
            pytest.skip(f'needs cgroup migration permission from {_cgroup(process.pid)} to {path}: {exc}')
    finally:
        _reap(process)


@pytest.mark.parametrize('error', [None, errno.EACCES, errno.EPERM, errno.EROFS, errno.EIO])
def test_migration_probe_classifies_kernel_errors_and_reaps(tmp_path, monkeypatch, error):
    from contextlib import contextmanager, nullcontext

    made = []
    real_spawn = _spawn

    def spawn():
        process = real_spawn()
        made.append(process)
        return process

    real_open = Path.open

    @contextmanager
    def open_control(path, *args, **kwargs):
        if path != tmp_path / 'cgroup.procs':
            with real_open(path, *args, **kwargs) as stream:
                yield stream
            return

        def write(value):
            assert value == f'{made[0].pid}\n'.encode()
            if error is not None:
                raise OSError(error, os.strerror(error))
        yield SimpleNamespace(write=write)

    monkeypatch.setattr(sys.modules[__name__], '_spawn', spawn)
    monkeypatch.setattr(Path, 'open', open_control)
    expected = nullcontext()
    if error == errno.EIO:
        expected = pytest.raises(OSError, match='Input/output error')
    elif error is not None:
        expected = pytest.raises(pytest.skip.Exception, match='needs cgroup migration permission')
    with expected:
        _probe_migration(tmp_path)
    assert len(made) == 1 and made[0].poll() is not None
    assert made[0].stdin.closed and made[0].stdout.closed


@pytest.fixture
def migration_access(run_scope):
    target = scope_mod.CGROUP_ROOT / _cgroup(run_scope.submitter.pid).lstrip('/')
    _probe_migration(target)


def test_child_and_descendant_inherit_run_scope_and_limit(run_scope, migration_access):
    process = scope_mod.placing_spawn(_spawn, run_scope.policy)()
    try:
        process.stdin.write(b'go\n')
        process.stdin.flush()
        descendant = int(process.stdout.readline())
        want = _cgroup(run_scope.submitter.pid)
        assert want.endswith(run_scope.unit)
        assert _cgroup(process.pid) == want and _cgroup(descendant) == want
        assert (scope_mod.CGROUP_ROOT / want.lstrip('/') / 'memory.max').read_text().strip() == str(64 * 1024 * 1024)
    finally:
        _reap(process)


def test_unplaced_spawn_stays_in_gateway_cgroup(run_scope):
    """Control (the pre-fix behaviour): the bare spawn is not in the run scope."""
    process = _spawn()
    try:
        assert _cgroup(process.pid) != _cgroup(run_scope.submitter.pid)
    finally:
        _reap(process)


def test_non_kanban_policy_unchanged(run_scope):
    for policy in (None, SimpleNamespace(kanban_json=None)):
        process = scope_mod.placing_spawn(_spawn, policy)()
        try:
            assert _cgroup(process.pid) == _cgroup(os.getpid())
        finally:
            _reap(process)


@pytest.mark.parametrize('mutate', ['claim', 'run', 'status'])
def test_stale_claim_fails_closed_before_spawn(run_scope, mutate):
    ctx = dict(run_scope.ctx)
    if mutate == 'claim':
        ctx['claim_lock'] = 'other:2'
    elif mutate == 'run':
        ctx['run_id'] = 8
    else:
        import sqlite3
        with sqlite3.connect(run_scope.db) as conn:
            conn.execute("UPDATE tasks SET status='ready'")
    spawned = []
    with pytest.raises(scope_mod.ScopePlacementError):
        scope_mod.placing_spawn(lambda: spawned.append(1), SimpleNamespace(kanban_json=json.dumps(ctx)))()
    assert spawned == []


def test_submitter_in_other_runs_scope_fails_closed(run_scope):
    import sqlite3
    with sqlite3.connect(run_scope.db) as conn:
        conn.execute("UPDATE task_runs SET worker_pid=?", (os.getpid(),))
    # our own cgroup is not a run scope: unchanged, not a failure
    assert scope_mod.resolve_run_scope(run_scope.policy) is None
    ctx = dict(run_scope.ctx, run_id=7)
    other = Path(run_scope.unit.replace('-run-7', '-run-9'))
    real, real_own = scope_mod._cgroup_path, scope_mod._own_cgroup
    scope_mod._cgroup_path = lambda pid: '/x/' + other.name
    scope_mod._own_cgroup = lambda: '/x/gateway.service'  # the gateway lives elsewhere
    try:
        with pytest.raises(scope_mod.ScopePlacementError, match='mismatch'):
            scope_mod.resolve_run_scope(SimpleNamespace(kanban_json=json.dumps(ctx)))
    finally:
        scope_mod._cgroup_path, scope_mod._own_cgroup = real, real_own


# Run inside a foreign run scope: point run 7's submitter at a pid in that scope, then resolve.
_FOREIGN_RESOLVE = """
import json, os, sqlite3, sys
from types import SimpleNamespace
from gateway import session_kanban_scope as scope_mod
ctx, submitter = json.loads(sys.argv[1]), int(sys.argv[2]) or os.getpid()
with sqlite3.connect(ctx['db']) as conn:
    conn.execute('UPDATE task_runs SET worker_pid=?', (submitter,))
try:
    print(json.dumps({'target': str(scope_mod.resolve_run_scope(SimpleNamespace(kanban_json=json.dumps(ctx))))}))
except scope_mod.ScopePlacementError as exc:
    print(json.dumps({'error': str(exc)}))
"""


@pytest.mark.parametrize('gateway_in_foreign_scope', [True, False])
def test_submitter_inheriting_enclosing_run_scope(run_scope, gateway_in_foreign_scope):
    """A dispatcher run unsupervised inside another worker's scope (a test from a worker shell)
    gives its submitter that enclosing scope. When the gateway shares it, the child is born there
    (no escape): unchanged. A submitter in another run's scope the gateway does not share fails closed."""
    foreign = run_scope.unit.replace('-run-7', '-run-9')
    runtime = f'/run/user/{os.getuid()}'
    env = dict(os.environ, XDG_RUNTIME_DIR=runtime, DBUS_SESSION_BUS_ADDRESS=f'unix:path={runtime}/bus',
               PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    holder = None
    try:
        if gateway_in_foreign_scope:
            argv = [sys.executable, '-c', _FOREIGN_RESOLVE, json.dumps(run_scope.ctx), '0']
        else:
            holder = subprocess.Popen(['systemd-run', '--user', '--scope', '--quiet', '--collect', '-u', foreign,
                                       'sleep', '120'], env=env, stderr=subprocess.DEVNULL)
            deadline = time.time() + 10
            while time.time() < deadline and not (_cgroup(holder.pid) or '').endswith(foreign):
                time.sleep(0.05)
            assert (_cgroup(holder.pid) or '').endswith(foreign)
            argv = [sys.executable, '-c', _FOREIGN_RESOLVE, json.dumps(run_scope.ctx), str(holder.pid)]
            foreign = foreign.replace('-run-9', '-run-11')  # the resolver itself runs in yet another run scope
        out = subprocess.run(['systemd-run', '--user', '--scope', '--quiet', '--collect', '-u', foreign, *argv],
                             env=env, capture_output=True, text=True, timeout=60, check=True)
        verdict = json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        if holder is not None:
            holder.kill()
            holder.wait()
    if gateway_in_foreign_scope:
        assert verdict == {'target': 'None'}, out.stderr
    else:
        assert verdict == {'error': 'kanban_scope_mismatch'}, out.stderr


def test_migration_failure_kills_and_reaps_child(run_scope, monkeypatch):
    made = []

    def spawn(*a, **k):
        made.append(_spawn())
        return made[-1]
    monkeypatch.setattr(scope_mod, 'migrate', lambda pid, target: (_ for _ in ()).throw(
        scope_mod.ScopePlacementError('kanban_scope_migrate_failed')))
    with pytest.raises(scope_mod.ScopePlacementError):
        scope_mod.placing_spawn(spawn, run_scope.policy)()
    assert made and made[0].poll() is not None  # reaped, not leaked
    assert made[0].stdin.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_early', [True, False])
async def test_cancel_interleaving_leaves_no_child(run_scope, monkeypatch, cancel_early, request):
    if not cancel_early:
        request.getfixturevalue('migration_access')
    from gateway.session_worker_spawn import acquire_process
    made = []

    def spawn(*a, **k):
        made.append(_spawn())
        return made[-1]
    real_migrate = scope_mod.migrate

    entered, release = threading.Event(), threading.Event()

    def slow_migrate(pid, target):
        entered.set()
        release.wait(timeout=10)
        real_migrate(pid, target)
    monkeypatch.setattr(scope_mod, 'migrate', slow_migrate)
    task = asyncio.create_task(acquire_process(scope_mod.placing_spawn(spawn, run_scope.policy)))
    await async_wait_until(entered.is_set, what='migration started')
    if cancel_early:
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await async_wait_until(lambda: made and made[0].poll() is not None, what='orphan reaped')
    else:
        release.set()
        process = await task
        try:
            assert _cgroup(process.pid) == _cgroup(run_scope.submitter.pid)
        finally:
            _reap(process)
    for child in made:
        _reap(child)


@pytest.mark.asyncio
async def test_spawn_registered_worker_places_real_child(run_scope, monkeypatch, migration_access):
    """The production spawn path (not just the helper) lands the Popen child in the run scope."""
    from gateway import session_managed_worker as managed
    authority = SimpleNamespace(profile_id='p', pending_results={}, _managed_spawning={})
    monkeypatch.setattr(managed, '_mark_spawning', lambda *a: authority._managed_spawning)
    monkeypatch.setattr(managed, '_worker_env', lambda a: dict(os.environ))
    monkeypatch.setattr(managed, 'WORKER_POPEN', {'start_new_session': True, 'close_fds': True})
    monkeypatch.setattr(managed, 'worker_log_path', lambda p: None, raising=False)
    spawned = await managed._spawn_registered_worker(
        authority, SimpleNamespace(session_id='s'), {'generation': 1}, run_scope.policy)
    worker, process = spawned
    try:
        assert _cgroup(process.pid) == _cgroup(run_scope.submitter.pid)
    finally:
        worker.close()


def test_unreadable_board_does_not_bypass_budget(run_scope):
    ctx = dict(run_scope.ctx, db=str(run_scope.tmp / 'missing.db'))
    with pytest.raises(scope_mod.ScopePlacementError, match='db_unreadable'):
        scope_mod.resolve_run_scope(SimpleNamespace(kanban_json=json.dumps(ctx)))



def test_pid_publication_after_scope_resolution_started(run_scope, monkeypatch):
    """A fast submitter can reach the owner before Popen returns/publishes its PID."""
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    real_connect = sqlite3.connect
    observed = threading.Event()
    with real_connect(run_scope.db) as conn:
        conn.execute('UPDATE task_runs SET worker_pid=NULL')

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(lambda sql: observed.set() if 'SELECT worker_pid' in sql else None)
        return conn

    monkeypatch.setattr(scope_mod.sqlite3, 'connect', connect)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(scope_mod.resolve_run_scope, run_scope.policy)
        assert observed.wait(5)
        assert not pending.done()
        with real_connect(run_scope.db) as conn:
            conn.execute('UPDATE task_runs SET worker_pid=?', (run_scope.submitter.pid,))
        assert pending.result(timeout=5).name == run_scope.unit


def test_dead_submitter_does_not_bypass_budget(run_scope):
    run_scope.submitter.kill()
    run_scope.submitter.wait()
    with pytest.raises(scope_mod.ScopePlacementError, match='submitter_missing'):
        scope_mod.resolve_run_scope(run_scope.policy)

