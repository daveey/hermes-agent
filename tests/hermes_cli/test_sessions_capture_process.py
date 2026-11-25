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


def _burn(seconds, then):
    # Burn child CPU (process_time, so a loaded host cannot make it sleep through the budget).
    return (f'import sys, time\nsys.stdin.buffer.read()\nend = time.process_time() + {seconds!r}\n'
            f'while time.process_time() < end:\n    pass\n{then}')


def _capture_child_stat():
    from hermes_cli import loop_accounting
    return loop_accounting.counters().get('sessions.capture.child', {})


def test_child_cpu_is_accounted_outside_the_owner_thread():
    import threading as _threading
    from hermes_cli import loop_accounting

    def owner_accounted():
        own = _threading.get_native_id()
        return sum(t['accounted_cpu_s'] for t in loop_accounting.thread_cpu() if t['tid'] == own)

    before, owner_before = _capture_child_stat(), owner_accounted()
    capture = CaptureProcess(command=command(_burn(.3, frame())))
    try:
        assert json.loads(capture('sessions', {}))['sessions'][0]['id'] == 'real-row'
    finally:
        capture.close()
    after = _capture_child_stat()
    assert after['runs'] == before.get('runs', 0) + 1
    assert after['errors'] == before.get('errors', 0)
    assert after['fallbacks'] == before.get('fallbacks', 0)  # the CPU was readable, not assumed 0
    assert after['cpu_s'] - before.get('cpu_s', 0) >= .25
    # The CPU belongs to another process: the owner thread must not count it as its own.
    assert owner_accounted() - owner_before < .1


def test_reaped_grandchild_cpu_is_accounted():
    # The capture child may fork helpers (git); CPU of those it reaped is the child's cost too.
    grandchild = ('import time\nend = time.process_time() + .4\n'
                  'while time.process_time() < end:\n    pass\n')
    code = ('import subprocess, sys\nsys.stdin.buffer.read()\n'
            f'subprocess.run([sys.executable, "-I", "-c", {grandchild!r}], check=True)\n' + frame())
    before = _capture_child_stat()
    capture = CaptureProcess(command=command(code))
    try:
        assert json.loads(capture('sessions', {}))['sessions'][0]['id'] == 'real-row'
    finally:
        capture.close()
    after = _capture_child_stat()
    assert after['errors'] == before.get('errors', 0)
    assert after['cpu_s'] - before.get('cpu_s', 0) >= .35


def test_failed_child_cpu_is_accounted_as_an_error():
    before = _capture_child_stat()
    # A 2 s budget: on a loaded runner a 0.5 s child can get under 0.1 CPU-s.
    capture = CaptureProcess(seconds=2, command=command(_burn(30, '')))
    with pytest.raises(CaptureUnavailable) as error:
        capture('sessions', {})
    assert error.value.reason == 'timeout'
    after = _capture_child_stat()
    assert after['runs'] == before.get('runs', 0) + 1
    assert after['errors'] == before.get('errors', 0) + 1
    assert after['error_kinds'].get('CaptureUnavailable', 0) == before.get('error_kinds', {}).get(
        'CaptureUnavailable', 0) + 1
    assert after['cpu_s'] - before.get('cpu_s', 0) >= .1


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


def test_canonical_worker_batch_keeps_all_seven_annotations(tmp_path, monkeypatch):
    from hermes_cli.web_routers import profiles
    from hermes_cli.sessions_capture_worker import capture_many
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
    payload = json.loads(capture_many([('sessions', selected()), ('sessions_sidebar', {})]))
    assert payload['results'][0]['body']['sessions'][0]['id'] == 'seven-family-row'
    assert payload['results'][1]['body']['recents']['sessions']
    assert set(calls) == set(names)


@pytest.mark.parametrize('body', [
    b'{"results":[]}',
    b'{"results":[{"body":{"sessions":[]}},"nope"]}',
    b'{"results":[{"oops":true}]}',
    b'{"results":[{"body":[]}]}',
    b'{"results":[{"unavailable":"timeout"}]}',
    b'{"nope":1}',
])
def test_capture_many_malformed_result_is_typed(body):
    capture = CaptureProcess(command=command(frame(body)))
    with pytest.raises(CaptureUnavailable) as error:
        capture.capture_many([('sessions', {})])
    assert error.value.reason == 'invalid_body'
    assert capture._child is None


def test_capture_many_oversized_response_kills_and_reaps(monkeypatch):
    from hermes_cli import sessions_capture_process as module
    original = module.spawn_owned
    children = []
    def recorded(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(module, 'spawn_owned', recorded)
    capture = CaptureProcess(seconds=.5, command=command(
        'import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b"\\x00\\x40\\x00\\x01"); sys.stdout.buffer.flush()'))
    with pytest.raises(CaptureUnavailable) as error:
        capture.capture_many([('sessions', {})])
    assert error.value.reason == 'response_bytes'
    assert children and all(child.poll() is not None for child in children)
    assert capture._child is None


def test_capture_many_timeout_kills_and_reaps(monkeypatch):
    from hermes_cli import sessions_capture_process as module
    original = module.spawn_owned
    children = []
    def recorded(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(module, 'spawn_owned', recorded)
    capture = CaptureProcess(seconds=.5, command=command('import time; time.sleep(30)'))
    started = time.monotonic()
    with pytest.raises(CaptureUnavailable) as error:
        capture.capture_many([('sessions', {}), ('sessions_sidebar', {})])
    assert error.value.reason == 'timeout'
    assert time.monotonic() - started < 3
    assert children and all(child.poll() is not None for child in children)
    assert capture._child is None


def test_capture_many_rejects_view_count_without_spawn():
    capture = CaptureProcess(command=['must-not-spawn'])
    with pytest.raises(CaptureUnavailable, match='request_views'):
        capture.capture_many([])
    with pytest.raises(CaptureUnavailable, match='request_views'):
        capture.capture_many([('sessions', {})] * 5)


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
        started = time.monotonic()
        batch = capture.capture_many([
            (name, selector_for(inspect.signature(reader), (), params))
            for name, reader, params in selectors])
        assert time.monotonic() - started < 5
        assert all(isinstance(body, bytes) for body in batch)
        assert len(json.loads(batch[0])['sessions']) == 100
        assert json.loads(batch[1])['recents']['sessions']
        print(f'cold-parent-spinner all-three: {time.monotonic() - started:.3f}s')
    finally:
        stop.set()
        spinner.join(3)
        capture.close()
