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
