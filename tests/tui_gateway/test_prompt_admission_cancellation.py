"""Known non-starts retire authority; uncertain scheduling never replays."""
import pytest
from concurrent.futures import ThreadPoolExecutor
import threading

from tests.tui_gateway.test_prompt_admission import call
from tui_gateway import server, methods_prompt_admission as admission

pytest_plugins = ['tests.tui_gateway.test_prompt_admission']


def test_failed_dispatch_retires_row_before_releasing_writer(owner, monkeypatch):
    db, session, params, started = owner
    released = []
    release = server._release_active_session_slot

    def check_release(s):
        released.append(True)
        assert not db.get_messages(params['stored_session_id'])
        return release(s)

    monkeypatch.setattr(server, '_release_active_session_slot', check_release)
    monkeypatch.setattr(server, '_start_session_work', lambda *a, **kw: None)
    receipt = call('prompt.admit', params)['result']
    assert receipt['dispatch'] == 'cancelled'
    assert released and not session['running']
    for _ in range(3):
        assert call('prompt.admit', params)['result'] == receipt
    assert not started
    assert not db.get_messages_as_conversation(params['stored_session_id'])
    # The audit row and operation identity survive; only authority is retired.
    with db._read_ctx() as conn:
        row = conn.execute('SELECT content, active FROM messages WHERE message_uid=?',
                           (receipt['message_uid'],)).fetchone()
        assert tuple(row) == (params['text'], 0)
    monkeypatch.setattr(server, '_release_active_session_slot', release)
    monkeypatch.setattr(server, '_start_session_work', lambda target, **kw: started.append(target) or True)
    sid = next(sid for sid, s in server._sessions.items() if s is session)
    assert 'result' in call('prompt.submit', {'session_id': sid, 'text': 'ordinary follow-up'})
    assert [r['content'] for r in db.get_messages(params['stored_session_id'])] == ['ordinary follow-up']


def test_cancellation_storage_failure_quarantines_until_readback(owner, monkeypatch):
    db, session, params, _ = owner
    real = admission.mark_dispatch_failed
    monkeypatch.setattr(server, '_start_session_work', lambda *a, **kw: None)
    def fail(*args):
        raise RuntimeError('storage unavailable')
    monkeypatch.setattr(admission, 'mark_dispatch_failed', fail)
    assert 'error' in call('prompt.admit', params)
    assert session['running'], 'do not admit ordinary history before authority retirement'
    monkeypatch.setattr(admission, 'mark_dispatch_failed', real)
    query = {k: v for k, v in params.items() if k != 'text'}
    assert call('prompt.admission.get', query)['result']['dispatch'] == 'cancelled'
    assert not session['running']
    assert not db.get_messages(params['stored_session_id'])


def test_scheduler_late_worker_cannot_execute_cancelled_admission(owner, monkeypatch):
    _, _, params, _ = owner
    workers = []
    executed = []
    def broken(target, **kw):
        workers.append(target)
        raise RuntimeError('scheduler failed before start')
    monkeypatch.setattr(server, '_start_session_work', broken)
    monkeypatch.setattr(server, '_run_after_agent_ready', lambda *a, **kw: executed.append(True))
    assert call('prompt.admit', params)['result']['dispatch'] == 'cancelled'
    workers[0]()
    assert not executed


def test_receipt_read_waits_for_scheduler_failure(owner, monkeypatch):
    _, _, params, _ = owner
    entered, finish, reading = threading.Event(), threading.Event(), threading.Event()
    def scheduler(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
    monkeypatch.setattr(server, '_start_session_work', scheduler)
    query = {k: v for k, v in params.items() if k != 'text'}
    def read():
        reading.set()
        return call('prompt.admission.get', query)
    with ThreadPoolExecutor(2) as pool:
        submitted = pool.submit(call, 'prompt.admit', params)
        assert entered.wait(5)
        fetched = pool.submit(read)
        assert reading.wait(5)
        try:
            assert not fetched.done(), 'readback exposed unsettled acceptance'
        finally:
            finish.set()
        assert submitted.result(5)['result']['dispatch'] == 'cancelled'
        assert fetched.result(5)['result']['dispatch'] == 'cancelled'


def test_scheduler_throw_after_worker_started_is_not_cancellation(owner, monkeypatch):
    db, _, params, _ = owner
    executed = []
    def uncertain(target, **kw):
        target()
        raise RuntimeError('reply failed after start')
    monkeypatch.setattr(server, '_start_session_work', uncertain)
    monkeypatch.setattr(server, '_run_after_agent_ready', lambda *a, **kw: executed.append(True))
    result = call('prompt.admit', params)['result']
    assert result['dispatch'] == 'unknown'
    assert call('prompt.admit', params)['result'] == result
    assert executed == [True]
    assert len(db.get_messages(params['stored_session_id'])) == 1
