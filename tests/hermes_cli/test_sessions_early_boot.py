"""Early capture keeps one owner through import failures and app adoption."""
from types import ModuleType, SimpleNamespace
import signal
import sys
import threading

from fastapi import FastAPI
import pytest

from hermes_cli import sessions_early_boot as early
from hermes_cli import sessions_native_startup as native


@pytest.fixture
def boot(monkeypatch):
    from hermes_cli import isolated_scope, process_identity, proof_home, sessions_capture_process, update_proxy_env
    from hermes_cli.web_routers import profiles
    calls = []

    class Reader:
        def __init__(self):
            calls.append('reader')

        def prepare(self):
            calls.append('prepare')

        def close(self):
            calls.append('reader-close')
    monkeypatch.setattr(sessions_capture_process, 'CaptureProcess', Reader)
    runtime = SimpleNamespace(start_background=lambda: calls.append('start'),
                              close=lambda: calls.append('close'))
    monkeypatch.setenv('HERMES_FACTS_SESSIONS', '1')
    monkeypatch.setattr(early.sys, 'platform', 'linux')
    monkeypatch.setattr(proof_home, 'assert_backend_home', lambda **kw: calls.append('preflight'))
    monkeypatch.setattr(update_proxy_env, 'restore_user_proxy_env', lambda: calls.append('proxy'))
    monkeypatch.setattr(process_identity, 'is_desktop_owned_backend', lambda: False)
    monkeypatch.setattr(isolated_scope, 'record_launch_scope', lambda **kw: calls.append(('scope', kw)))
    monkeypatch.setattr(profiles, '_profile_targets', lambda reason: calls.append('targets') or [('default', '/unused')])
    monkeypatch.setattr(native, 'SessionsRuntime', lambda *a, capture_reader: runtime)
    return calls, runtime


@pytest.mark.parametrize('error', [ImportError, SystemExit, OSError, KeyboardInterrupt])
def test_pre_adoption_failures_close(boot, error):
    calls, _ = boot
    with pytest.raises(error):
        with early.early_sessions(headless=True, isolated=True):
            raise error()
    assert calls == ['preflight', 'proxy', ('scope', {'isolated': True, 'desktop_owned': False}),
                     'reader', 'prepare', 'targets', 'start', 'close']


@pytest.mark.asyncio
async def test_adoption_no_duplicate_start_or_close(boot):
    calls, runtime = boot
    app = FastAPI()
    with early.early_sessions(headless=True, isolated=False) as owner:
        app.state.early_sessions_owner = owner
        async with native.lifespan(app):
            assert app.state.sessions_facts is runtime
            with pytest.raises(RuntimeError, match='consumed'):
                owner.adopt()
        assert owner.closed
        assert app.state.sessions_facts is None
    assert calls.count('start') == calls.count('close') == 1


@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGINT])
def test_signal_in_import_gap_drains_and_chains(boot, signum):
    calls, _ = boot
    original = signal.getsignal(signum)
    def previous(sig, frame):
        calls.append('previous')
        raise SystemExit(123)
    signal.signal(signum, previous)
    try:
        with pytest.raises(SystemExit, match='123'):
            with early.early_sessions(headless=True, isolated=False):
                signal.getsignal(signum)(signum, None)
        assert calls[-2:] == ['close', 'previous']
        assert signal.getsignal(signum) is previous
    finally:
        signal.signal(signum, original)


@pytest.mark.parametrize('mode', ['legacy', 'dashboard', 'darwin'])
def test_rollback_does_not_touch_runtime(boot, monkeypatch, mode):
    calls, _ = boot
    if mode == 'legacy':
        monkeypatch.setenv('HERMES_FACTS_SESSIONS', '0')
    if mode == 'darwin':
        monkeypatch.setattr(early.sys, 'platform', 'darwin')  # os-marker: ok — unsupported-watch rollback only; no macOS APIs
    with early.early_sessions(headless=mode != 'dashboard', isolated=False) as owner:
        assert owner is None
    assert not calls


def test_ignored_signal_keeps_owner_adoptable(boot):
    calls, runtime = boot
    original = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with early.early_sessions(headless=True, isolated=False) as owner:
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            assert 'close' not in calls
            assert owner.adopt() is runtime
        assert calls.count('close') == 1
    finally:
        signal.signal(signal.SIGINT, original)


def test_worker_thread_skips_signal_install_and_still_starts(boot):
    calls, runtime = boot
    original_term = signal.getsignal(signal.SIGTERM)
    original_int = signal.getsignal(signal.SIGINT)
    result = {}

    def run():
        with early.early_sessions(headless=True, isolated=True) as owner:
            assert owner is not None
            result['owner'] = owner
            result['term'] = signal.getsignal(signal.SIGTERM)
            result['int'] = signal.getsignal(signal.SIGINT)
            assert owner.adopt() is runtime

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    assert result.get('owner') is not None
    assert result['term'] is original_term
    assert result['int'] is original_int
    assert signal.getsignal(signal.SIGTERM) is original_term
    assert signal.getsignal(signal.SIGINT) is original_int
    assert calls.count('start') == calls.count('close') == 1


def test_failed_drain_retains_owner():
    calls = []
    def close():
        calls.append('close')
        if len(calls) == 1:
            raise TimeoutError('retained')
    owner = early.EarlySessions(SimpleNamespace(close=close))
    with pytest.raises(TimeoutError):
        owner.close()
    assert not owner.closed
    owner.close()
    assert owner.closed
    with pytest.raises(RuntimeError):
        owner.adopt()


class _Startup(ModuleType):
    """Stands in for sessions_native_startup: the from-import of SessionsRuntime is its import point."""
    def __init__(self, calls, factory):
        super().__init__('hermes_cli.sessions_native_startup')
        self.__dict__['_calls'], self.__dict__['_factory'] = calls, factory

    def __getattr__(self, name):
        if name != 'SessionsRuntime':
            raise AttributeError(name)
        self._calls.append('startup-import')
        return self._factory()


def test_standby_is_prepared_before_startup_imports_and_handed_to_the_runtime(boot, monkeypatch):
    calls, runtime = boot
    readers = []

    def construct(profiles, targets, *, capture_reader):
        calls.append('runtime')
        readers.append(capture_reader)
        return runtime
    monkeypatch.setitem(sys.modules, 'hermes_cli.sessions_native_startup', _Startup(calls, lambda: construct))
    with early.early_sessions(headless=True, isolated=False) as owner:
        assert owner.runtime is runtime
    assert calls[3:] == ['reader', 'prepare', 'startup-import', 'targets', 'runtime', 'start', 'close']
    assert type(readers[0]).__name__ == 'Reader'
    assert 'reader-close' not in calls  # the runtime owns the reader once it exists


@pytest.mark.parametrize('stage', ['import', 'construct'])
def test_failure_before_the_owner_exists_closes_the_reader(boot, monkeypatch, stage):
    calls, _ = boot

    def failed_import():
        raise ImportError('startup')

    def failed_construct(*args, **kwargs):
        raise RuntimeError('targets')
    factory = failed_import if stage == 'import' else lambda: failed_construct
    monkeypatch.setitem(sys.modules, 'hermes_cli.sessions_native_startup', _Startup(calls, factory))
    with pytest.raises(ImportError if stage == 'import' else RuntimeError):
        with early.early_sessions(headless=True, isolated=False):
            pytest.fail('must not yield')
    assert calls.index('prepare') < calls.index('startup-import')
    assert calls[-1] == 'reader-close' and 'start' not in calls and 'close' not in calls


@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGINT])
def test_signal_before_the_owner_exists_closes_the_reader(boot, monkeypatch, signum):
    calls, _ = boot
    from hermes_cli.web_routers import profiles
    original = signal.getsignal(signum)

    def previous(sig, frame):
        calls.append('previous')
        raise SystemExit(123)

    def targets(reason):
        signal.getsignal(signum)(signum, None)  # delivered between prepare and the runtime
    monkeypatch.setattr(profiles, '_profile_targets', targets)
    signal.signal(signum, previous)
    try:
        with pytest.raises(SystemExit, match='123'):
            with early.early_sessions(headless=True, isolated=False):
                pytest.fail('must not yield')
        assert calls[3:] == ['reader', 'prepare', 'reader-close', 'previous', 'reader-close']
        assert signal.getsignal(signum) is previous
    finally:
        signal.signal(signum, original)


# The real CaptureProcess under the real handler, with a real signal sent to this process from
# inside one of the reader's two slow steps. Only admission, the profile lookup, the runtime (its
# close closes the reader, as the real one does) and the worker's command are stand-ins.
BOOT_SIGNAL = '''
import faulthandler, json, os, signal, sys
from types import SimpleNamespace
faulthandler.dump_traceback_later(20, exit=True)  # a deadlock ends as a traceback, not a hang
window, signum, handler = sys.argv[1], int(sys.argv[2]), sys.argv[3]
from hermes_cli import (isolated_scope, loop_accounting, process_identity, proof_home, update_proxy_env,
                        sessions_capture_process as capture, sessions_early_boot as early,
                        sessions_native_startup as native)
from hermes_cli.web_routers import profiles
proof_home.assert_backend_home = lambda **kw: None
update_proxy_env.restore_user_proxy_env = lambda: None
process_identity.is_desktop_owned_backend = lambda: False
isolated_scope.record_launch_scope = lambda **kw: None
profiles._profile_targets = lambda reason: [('default', '/unused')]
native.SessionsRuntime = lambda profiles, targets, *, capture_reader: SimpleNamespace(
    start_background=lambda: None, close=lambda: runtime_close(capture_reader))
import threading
feed_lock = threading.Lock()  # not re-entrant, like the runtime's FeedLease close lock

def runtime_close(capture_reader):
    with feed_lock:
        if window == 'runtime-close' and 'signal' not in events:  # the runtime is closing its feed
            events.append('signal')
            os.kill(os.getpid(), signum)
            events.append('feed-closed')
        capture_reader.close()
capture.runtime_command = lambda root, module: [sys.executable, '-I', '-c', 'import sys; sys.stdin.buffer.read()']
capture._generation = lambda: 'boot'
events, children, stopped = [], [], []
spawn, kill_tree, survivors = capture.spawn_owned, capture.kill_process_tree, capture.stop_owned_group_survivors

def spawned(*args, **kwargs):
    children.append(spawn(*args, **kwargs))
    if window.startswith('prepare'):  # the child exists and prepare() has not been handed it yet
        events.append('signal')
        os.kill(os.getpid(), signum)
        if window in ('prepare-both', 'prepare-twice'):  # a second signal held behind the first
            events.append('signal')
            os.kill(os.getpid(), other if window == 'prepare-both' else signum)
        events.append('spawned')
    return children[-1]

def killing(proc, **kwargs):
    if window == 'close' and 'signal' not in events:  # close() is retiring the standby, still alive
        events.append('signal')
        os.kill(os.getpid(), signum)
    elif window == 'prepare-then-close' and events.count('signal') == 1:
        # The signal held during prepare() is being acted on (its close is retiring the standby):
        # a second, different one lands inside that delivery.
        events.append('signal')
        os.kill(os.getpid(), other)
    return kill_tree(proc, **kwargs)

def stopping(pid, **kwargs):
    stopped.append(pid)
    return survivors(pid, **kwargs)

def previous(sig, frame):
    events.append('previous' if sig == signum else 'previous-other')
    if handler == 'exits':
        raise SystemExit(123 if sig == signum else 124)
capture.spawn_owned, capture.kill_process_tree, capture.stop_owned_group_survivors = spawned, killing, stopping
other = signal.SIGINT if signum == signal.SIGTERM else signal.SIGTERM
signal.signal(signum, previous)
signal.signal(other, previous)
code = None
try:
    with early.early_sessions(headless=True, isolated=False) as owner:
        events.append('yielded')
except SystemExit as stop:
    code = stop.code
faulthandler.cancel_dump_traceback_later()
counted = loop_accounting.counters().get('sessions.capture.child', {})
print(json.dumps({'events': events, 'code': code, 'restored': signal.getsignal(signum) is previous,
                  'children': [child.pid for child in children], 'stopped': stopped,
                  'alive': [child.pid for child in children if child.poll() is None],
                  'runs': counted.get('runs', 0), 'skips': counted.get('skips', 0)}))
'''


def _boot_signal(window, signum, handler):
    import json
    import os
    import subprocess
    from hermes_cli._startup_fast import project_root_str
    done = subprocess.run([sys.executable, '-c', BOOT_SIGNAL, window, str(int(signum)), handler],
                          cwd=project_root_str(), env=dict(os.environ, HERMES_FACTS_SESSIONS='1'),
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr[-3000:]  # a deadlock exits 1 with every thread's stack
    return json.loads(done.stdout.splitlines()[-1])


def _stopped_and_accounted_once(seen):
    assert len(seen['children']) == 1 and not seen['alive']
    assert seen['children'][0] in seen['stopped']
    assert seen['runs'] == 1 and seen['skips'] == 1
    assert seen['restored']


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGINT])
def test_signal_while_the_standby_spawns_stops_the_child_and_chains(signum):
    seen = _boot_signal('prepare', signum, 'exits')
    # The spawn finishes, so the reader owns the child before the signal is acted on.
    assert seen['events'] == ['signal', 'spawned', 'previous'] and seen['code'] == 123
    _stopped_and_accounted_once(seen)


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGINT])
def test_signal_while_close_retires_the_standby_stops_the_child_and_chains(signum):
    seen = _boot_signal('close', signum, 'exits')
    assert seen['events'] == ['yielded', 'signal', 'previous'] and seen['code'] == 123
    _stopped_and_accounted_once(seen)


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('window,events', [('prepare', ['signal', 'spawned', 'previous', 'yielded']),
                                           ('close', ['yielded', 'signal', 'previous'])])
def test_signal_whose_previous_handler_returns_leaves_no_child_behind(window, events):
    # The interrupted step runs on afterwards: it must neither hang nor stop or account the child again.
    seen = _boot_signal(window, signal.SIGTERM, 'returns')
    assert seen['events'] == events and seen['code'] is None
    _stopped_and_accounted_once(seen)


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize('handler,code,tail', [('returns', None, ['yielded']), ('exits', 124, [])])
def test_two_distinct_signals_held_during_prepare_are_both_delivered_in_order(signum, handler, code, tail):
    # Held behind the first, the second must not be lost; when the first handler raises, the second
    # still runs (as an unheld pending signal would) and its exit is the one that propagates.
    seen = _boot_signal('prepare-both', signum, handler)
    assert seen['events'] == ['signal', 'signal', 'spawned', 'previous', 'previous-other', *tail]
    assert seen['code'] == code
    _stopped_and_accounted_once(seen)


@pytest.mark.platforms('linux')
def test_a_repeated_signal_held_during_prepare_is_delivered_once():
    seen = _boot_signal('prepare-twice', signal.SIGTERM, 'returns')
    assert seen['events'] == ['signal', 'signal', 'spawned', 'previous', 'yielded'] and seen['code'] is None
    _stopped_and_accounted_once(seen)


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize('handler,code,tail', [('returns', None, ['yielded']), ('exits', 124, [])])
def test_a_signal_arriving_while_a_held_one_is_delivered_runs_after_it(signum, handler, code, tail):
    # The second lands inside the first's close (retiring the standby), after the hold of prepare()
    # ended: it is neither lost nor run inside that close, and runs after the first handler.
    seen = _boot_signal('prepare-then-close', signum, handler)
    assert seen['events'] == ['signal', 'spawned', 'signal', 'previous', 'previous-other', *tail]
    assert seen['code'] == code
    _stopped_and_accounted_once(seen)


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGINT])
def test_signal_while_the_runtime_closes_does_not_reenter_its_close(signum):
    # The runtime's close holds a lock that is not re-entrant (FeedLease): a handler that closed
    # again from inside it would hang. The signal is acted on once that close has returned.
    seen = _boot_signal('runtime-close', signum, 'exits')
    assert seen['events'] == ['yielded', 'signal', 'feed-closed', 'previous'] and seen['code'] == 123
    _stopped_and_accounted_once(seen)
