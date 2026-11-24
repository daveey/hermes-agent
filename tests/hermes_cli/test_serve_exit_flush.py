"""No agent import at boot; loaded gateways still receive the full exit flush."""
import signal
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('initializing', [True, False])
def test_lazy_flush_only_calls_fully_initialized_gateway(monkeypatch, initializing):
    from hermes_cli import serve_exit_flush as bridge
    calls = []
    gateway = SimpleNamespace(__spec__=SimpleNamespace(_initializing=initializing),
                              _flush_sessions_before_exit=lambda: calls.append('flush'))
    with monkeypatch.context() as scoped:
        scoped.setitem(sys.modules, 'tui_gateway.server', gateway)
        bridge.dispatch(signal.SIGTERM, None, lambda *a: calls.append('previous'))
    assert calls == (['previous'] if initializing else ['flush', 'previous'])


def test_lazy_flush_respects_ignored_signal(monkeypatch):
    from hermes_cli import serve_exit_flush as bridge
    gateway = SimpleNamespace(__spec__=None,
                              _flush_sessions_before_exit=lambda *a: pytest.fail('ignored signal flushed'))
    with monkeypatch.context() as scoped:
        scoped.setitem(sys.modules, 'tui_gateway.server', gateway)
        bridge.dispatch(signal.SIGINT, None, signal.SIG_IGN)


def test_install_off_main_thread_is_inert(monkeypatch):
    from hermes_cli import serve_exit_flush as bridge
    monkeypatch.setattr(bridge.threading, 'current_thread', object)
    monkeypatch.setattr(bridge.signal, 'signal', lambda *a: pytest.fail('off-main installation'))
    assert bridge.install() is False


def test_install_signal_failure_preserves_available_handler(monkeypatch):
    from hermes_cli import serve_exit_flush as bridge
    calls = []

    def install(signum, callback):
        calls.append(signum)
        if signum == signal.SIGTERM:
            raise OSError('unsupported')
        return signal.SIG_IGN

    monkeypatch.setattr(bridge, '_previous', {})
    monkeypatch.setattr(bridge.signal, 'getsignal', lambda signum: signal.SIG_DFL)
    monkeypatch.setattr(bridge.signal, 'signal', install)
    assert bridge.install() is True
    assert calls == [signal.SIGTERM, signal.SIGINT]
    assert bridge._previous == {signal.SIGINT: signal.SIG_IGN}


def test_default_disposition_falls_back_to_signal_exit_code(monkeypatch):
    from hermes_cli import serve_exit_flush as bridge
    monkeypatch.delitem(sys.modules, 'tui_gateway.server', raising=False)
    monkeypatch.setattr(bridge.signal, 'signal', lambda *a: None)

    def refused(*args):
        raise OSError('signal unavailable')

    monkeypatch.setattr(bridge.os, 'kill', refused)
    with pytest.raises(SystemExit) as stopped:
        bridge.dispatch(signal.SIGTERM, None, signal.SIG_DFL)
    assert stopped.value.code == 128 + signal.SIGTERM


@pytest.mark.platforms('posix')
def test_installed_bridge_flushes_real_gateway_in_terminated_process(tmp_path):
    import os
    import subprocess
    from pathlib import Path
    from hermes_state import SessionDB

    home = tmp_path / 'home'
    home.mkdir()
    code = r'''
import os, signal, sys, faulthandler
faulthandler.dump_traceback_later(15)
from pathlib import Path
from hermes_cli import serve_exit_flush
assert 'tui_gateway.server' not in sys.modules
home = Path(os.environ['HERMES_HOME'])
def previous(signum, frame):
    (home / 'previous').write_text('called')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, previous)
assert serve_exit_flush.install()
assert serve_exit_flush.install()  # no self-chain
assert 'tui_gateway.server' not in sys.modules
from hermes_state import SessionDB
from tui_gateway import server
db = SessionDB(db_path=home / 'state.db')
db.create_session('fixture-exit', source='tui')
class Agent:
    session_id = 'fixture-exit'
    _session_messages = [{'role': 'user', 'content': 'survive actual termination'}]
    def _persist_session(self, messages, conversation_history=None):
        for message in messages:
            if not message.get('_db_persisted'):
                db.append_message(self.session_id, message['role'], message['content'])
                message['_db_persisted'] = True
server._sessions['fixture-exit'] = {'agent': Agent(), 'session_key': 'fixture-exit', 'running': False}
faulthandler.cancel_dump_traceback_later()
os.write(1, b'READY\n')
signal.pause()
'''
    env = dict(os.environ, HOME=str(home), HERMES_HOME=str(home), HERMES_TEST_ISOLATION='1')
    root = Path(__file__).resolve().parents[2]
    diagnostics = (tmp_path / 'child.log').open('w+')
    child = subprocess.Popen([sys.executable, '-c', code], cwd=root, env=env,
                             stdout=subprocess.PIPE, stderr=diagnostics, text=True, encoding='utf-8')
    try:
        # A bounded reader thread avoids a hung test when child import fails.
        import concurrent.futures
        assert child.stdout is not None
        with concurrent.futures.ThreadPoolExecutor() as reader:
            ready = reader.submit(child.stdout.readline)
            try:
                assert ready.result(timeout=30).strip() == 'READY'
            except BaseException as exc:
                child.kill()
                child.wait(timeout=10)
                diagnostics.seek(0)
                raise AssertionError(diagnostics.read()) from exc
        child.send_signal(signal.SIGTERM)
        out, _ = child.communicate(timeout=30)
        diagnostics.seek(0)
        err = diagnostics.read()
        assert child.returncode == 0, (out, err)
        assert 'Exception ignored' not in err and 'Traceback' not in err, err
        assert (home / 'previous').read_text() == 'called'
        db = SessionDB(db_path=home / 'state.db')
        try:
            assert any(row['content'] == 'survive actual termination' for row in db.get_messages('fixture-exit'))
        finally:
            db.close()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
        diagnostics.close()
