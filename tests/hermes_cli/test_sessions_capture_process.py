"""Real private children: framing, elapsed budget, crashes and custody."""
import json

import struct
import sys
import threading
import time

import pytest

from hermes_cli.sessions_capture_process import CaptureProcess, CaptureUnavailable, MAX_REQUEST

pytestmark = pytest.mark.platforms('linux')


def command(code):
    return [sys.executable, '-I', '-c', code]


def frame(body=b'{"sessions":[{"id":"real-row"}]}'):
    return f'import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write({struct.pack("!I", len(body)) + body!r}); sys.stdout.buffer.flush()'


def test_success_and_fresh_restart():
    capture = CaptureProcess(command=command(frame()))
    for _ in range(2):
        assert json.loads(capture('sessions', {}))['sessions'][0]['id'] == 'real-row'
        assert capture._child is None
    capture.close()
    with pytest.raises(CaptureUnavailable, match='closed'):
        capture('sessions', {})


@pytest.mark.parametrize('code,reason', [
    ('import time; time.sleep(30)', 'timeout'),
    ('import sys, os; sys.stdin.buffer.read(); os._exit(7)', 'crash_or_truncated_frame'),
    ('import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b"\\x00\\x40\\x00\\x01"); sys.stdout.buffer.flush()', 'response_bytes'),
    (frame(b'not json'), 'transport'),
    (frame(b'[]'), 'invalid_body'),
    (frame() + '; sys.stdout.buffer.write(b"x"); sys.stdout.buffer.flush()', 'trailing_frame'),
    ('import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b"\\x00\\x00"); sys.stdout.buffer.flush()', 'crash_or_truncated_frame'),
])
def test_faults_are_typed_and_reaped(code, reason, monkeypatch):
    from hermes_cli import sessions_capture_process as module
    original = module.spawn_owned
    children = []
    def recorded(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(module, 'spawn_owned', recorded)
    capture = CaptureProcess(seconds=.5, command=command(code))
    started = time.monotonic()
    with pytest.raises(CaptureUnavailable) as error:
        capture('sessions', {})
    assert error.value.reason == reason
    assert time.monotonic() - started < 3
    assert children and all(child.poll() is not None for child in children)
    assert capture._child is None


def test_request_limit_precedes_spawn():
    capture = CaptureProcess(command=['must-not-spawn'])
    with pytest.raises(CaptureUnavailable, match='request_bytes'):
        capture('sessions', {'query': 'x' * MAX_REQUEST})


def test_close_cancels_inflight_and_prevents_replacement():
    capture = CaptureProcess(seconds=30, command=command('import time; time.sleep(30)'))
    errors = []
    def run():
        try:
            capture('sessions', {})
        except CaptureUnavailable as error:
            errors.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 3
    while capture._child is None and time.monotonic() < deadline:
        time.sleep(.01)
    child = capture._child
    assert child is not None
    capture.close()
    thread.join(3)
    assert not thread.is_alive() and errors
    assert child.poll() is not None
    with pytest.raises(CaptureUnavailable, match='closed'):
        capture('sessions', {})


def test_default_canonical_worker_real_store(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HOME', str(home))
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('canonical-row', source='cli', model='test')
    db.close()
    from hermes_cli import sessions_capture_process as module
    original = module.spawn_owned
    diagnostics = tmp_path / 'child-stderr'
    def diagnostic_spawn(*args, **kwargs):
        with diagnostics.open('wb') as output:
            kwargs['stderr'] = output
            return original(*args, **kwargs)
    monkeypatch.setattr(module, 'spawn_owned', diagnostic_spawn)
    capture = CaptureProcess()
    try:
        try:
            from tests.hermes_cli.test_sessions_native_startup import selected
            result = json.loads(capture('sessions', selected(limit=20)))
        except CaptureUnavailable:
            pytest.fail(diagnostics.read_text())
        assert any(row['id'] == 'canonical-row' for row in result['sessions'])
    finally:
        capture.close()


def test_spawn_failure_is_typed():
    with pytest.raises(CaptureUnavailable, match='spawn'):
        CaptureProcess(command=['/nonexistent/capture-worker'])('sessions', {})


def test_high_descriptor_frame_read():
    import fcntl
    import os
    from hermes_cli.sessions_capture_process import _read_exact
    reader, writer = os.pipe()
    high = fcntl.fcntl(reader, fcntl.F_DUPFD_CLOEXEC, 1100)
    try:
        os.write(writer, b'abcd')
        assert _read_exact(high, 4, time.monotonic() + 1) == b'abcd'
    finally:
        for fd in (reader, writer, high):
            os.close(fd)


def test_canonical_worker_keeps_all_seven_annotations(tmp_path, monkeypatch):
    from hermes_cli.web_routers import profiles
    from hermes_cli.sessions_capture_worker import capture
    from tests.hermes_cli.test_sessions_native_startup import selected
    from tests.hermes_cli.test_profiles_sidebar_scope import _seed_session
    home = tmp_path / 'home'
    home.mkdir()
    _seed_session(home, 'seven-family-row', source='cli')
    monkeypatch.setattr(profiles, '_profile_targets', lambda *a: [('default', home)])
    calls = []
    names = ('kanban_activity', 'goal_activity', 'cron_activity', 'background_activity',
             'turn_activity', 'subagent_activity', 'review_wait')
    for name in names:
        original = getattr(profiles, 'annotate_' + name)
        def recorded(rows, *args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(rows, *args, **kwargs)
        monkeypatch.setattr(profiles, 'annotate_' + name, recorded)
    assert json.loads(capture('sessions', selected()))['sessions'][0]['id'] == 'seven-family-row'
    assert set(calls) == set(names)


def test_cold_twelve_profile_capture_under_parent_gil_contention(tmp_path, monkeypatch):
    import inspect
    import sqlite3
    from hermes_state import SessionDB
    from hermes_cli.sessions_fact_routes import selector_for
    from hermes_cli.web_routers import profiles
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(home))
    for index in range(12):
        target = home if index == 0 else home / 'profiles' / f'profile-{index}'
        target.mkdir(parents=True)
        (target / 'config.yaml').write_text('{}\n', encoding='utf-8')
        db = SessionDB(db_path=target / 'state.db')
        db.create_session(f'row-{index}', source='cli', model='test')
        db.append_message(f'row-{index}', role='user', content='hello')
        db.close()
        with sqlite3.connect(target / 'state.db') as conn:
            conn.executemany('INSERT INTO sessions (id, source, started_at, message_count) VALUES (?, ?, ?, ?)',
                             [(f'bulk-{index}-{n}', 'cli', time.time() - n, 1) for n in range(2000)])
    stop = threading.Event()
    def spin():
        while not stop.is_set():
            pass
    spinner = threading.Thread(target=spin)
    spinner.start()
    capture = CaptureProcess()
    try:
        # Each call launches a COLD worker, including imports and serialization.
        selectors = [('sessions', profiles.get_profiles_sessions, {'limit': 100}),
                     ('sessions_sidebar', profiles.get_profiles_sessions_sidebar, {}),
                     ('sessions_stamps', profiles.get_profiles_session_stamps,
                      {'profile': 'default', 'ids': 'row-0'})]
        for name, reader, params in selectors:
            started = time.monotonic()
            result = json.loads(capture(name, selector_for(inspect.signature(reader), (), params)))
            assert time.monotonic() - started < 5
            assert result and 'error' not in result
            print(f'cold-parent-spinner {name}: {time.monotonic() - started:.3f}s')
            if name == 'sessions':
                assert len(result['sessions']) == 100
                assert result['total'] >= 24000
            elif name == 'sessions_sidebar':
                assert result['recents']['sessions']
    finally:
        stop.set()
        spinner.join(3)
        capture.close()
