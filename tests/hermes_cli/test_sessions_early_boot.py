"""Early capture keeps one owner through import failures and app adoption."""
from types import SimpleNamespace
import signal

from fastapi import FastAPI
import pytest

from hermes_cli import sessions_early_boot as early
from hermes_cli import sessions_native_startup as native


@pytest.fixture
def boot(monkeypatch):
    from hermes_cli import isolated_scope, process_identity, proof_home, update_proxy_env
    from hermes_cli.web_routers import profiles
    calls = []
    runtime = SimpleNamespace(start_background=lambda: calls.append('start'),
                              close=lambda: calls.append('close'))
    monkeypatch.setenv('HERMES_FACTS_SESSIONS', '1')
    monkeypatch.setattr(early.sys, 'platform', 'linux')
    monkeypatch.setattr(proof_home, 'assert_backend_home', lambda **kw: calls.append('preflight'))
    monkeypatch.setattr(update_proxy_env, 'restore_user_proxy_env', lambda: calls.append('proxy'))
    monkeypatch.setattr(process_identity, 'is_desktop_owned_backend', lambda: False)
    monkeypatch.setattr(isolated_scope, 'record_launch_scope', lambda **kw: calls.append(('scope', kw)))
    monkeypatch.setattr(profiles, '_profile_targets', lambda reason: calls.append('targets') or [('default', '/unused')])
    monkeypatch.setattr(native, 'SessionsRuntime', lambda *a: runtime)
    return calls, runtime


@pytest.mark.parametrize('error', [ImportError, SystemExit, OSError, KeyboardInterrupt])
def test_pre_adoption_failures_close(boot, error):
    calls, _ = boot
    with pytest.raises(error):
        with early.early_sessions(headless=True, isolated=True):
            raise error()
    assert calls == ['preflight', 'proxy', ('scope', {'isolated': True, 'desktop_owned': False}), 'targets', 'start', 'close']


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
