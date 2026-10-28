"""Failure injection at the update pause's durable notification boundary."""
import types

from tests.tui_gateway.test_update_pause_killed_jobs import (
    _job, _install, _session, _request_pause,
)

pytest_plugins = ["tests.tui_gateway.test_update_pause_killed_jobs"]
from tui_gateway import update_pause as up, turn_marker as tm, server
from tools.process_registry_notifications import format_process_notification


def test_write_failure_does_not_kill_or_release(home, registry, monkeypatch):
    proc = _job('proc_write_fail', owner='turn', command='sleep 600', notify=True)
    _install(registry, proc)
    tm.record_turn_start(home, 'session-key', 'wait')
    _request_pause(home)
    def fail(*a, **k):
        raise OSError('disk unavailable')
    monkeypatch.setattr(tm, 'atomic_json_write', fail)
    session = _session(agent=types.SimpleNamespace(_process_owner_task_ids=('turn',)), profile_home=str(home))
    assert up.boundary_pause_hook(session, 'session-key')() is None
    assert not proc.exited


def test_kill_failure_refuses_pause_and_retains_intent(home, registry, monkeypatch):
    proc = _job('proc_fail', owner='turn', command='sleep 600')
    _install(registry, proc)
    tm.record_turn_start(home, 'session-key', 'wait')
    _request_pause(home)
    monkeypatch.setattr(registry, 'kill_process', lambda *a, **k: {'status': 'error'})
    session = _session(agent=types.SimpleNamespace(_process_owner_task_ids=('turn',)), profile_home=str(home))
    assert up.boundary_pause_hook(session, 'session-key')() is None
    job, = tm.read_turn_marker(home, 'session-key')['killed_jobs']
    assert job['session_id'] == proc.id and job['kill_state'] == 'pending'


def test_receipt_and_normal_notice_are_redacted_and_name_update(home, registry):
    secret = 'sk-ant-api03-' + 'x' * 100
    proc = _job('proc_secret', owner='turn', command='echo ' + secret, notify=True)
    proc.output_buffer = secret
    _install(registry, proc)
    tm.record_turn_start(home, 'session-key', 'wait')
    _request_pause(home)
    session = _session(agent=types.SimpleNamespace(_process_owner_task_ids=('turn',)), profile_home=str(home))
    assert up.boundary_pause_hook(session, 'session-key')() == up.PAUSE_REASON
    persisted = (home / 'desktop' / 'interrupted_turns.json').read_text()
    assert secret not in persisted
    note = up.pause_note('wait', tm.read_turn_marker(home, 'session-key')['killed_jobs'])
    assert secret not in note
    event = registry.completion_queue.get_nowait()
    assert 'killed by the Hermes update' in format_process_notification(event)


def test_retry_marker_retains_interruption_receipts(home):
    job = {'session_id': 'proc_dead', 'command': 'sleep 600', 'started_at': 123,
           'kill_state': 'killed', 'notify_on_complete': True, 'exit_code': -15}
    session = _session(profile_home=str(home), _auto_continue_prompt='wait',
                       _auto_continue_killed_jobs=[job], _auto_continue_attempt=0, _auto_continue_pauses=1)
    server._record_turn_marker(session, 'continuation')
    assert tm.read_turn_marker(home, 'session-key')['killed_jobs'] == [job]


def test_write_after_signal_failure_leaves_uncertain_receipt(home, registry, monkeypatch):
    proc = _job('proc_second_write', owner='turn', command='sleep 600', notify=True)
    _install(registry, proc)
    tm.record_turn_start(home, 'session-key', 'wait')
    _request_pause(home)
    write = tm.atomic_json_write
    calls = []
    def fail_second(*a, **k):
        calls.append(1)
        if len(calls) > 1:
            raise OSError('disk unavailable after kill')
        return write(*a, **k)
    monkeypatch.setattr(tm, 'atomic_json_write', fail_second)
    session = _session(agent=types.SimpleNamespace(_process_owner_task_ids=('turn',)), profile_home=str(home))
    assert up.boundary_pause_hook(session, 'session-key')() is None
    assert proc.exited
    job, = tm.read_turn_marker(home, 'session-key')['killed_jobs']
    assert job['kill_state'] == 'pending'
    assert proc.id in up.pause_note('wait', [job])
