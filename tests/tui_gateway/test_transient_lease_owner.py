"""A lease fence is not evidence that a person must take a chat over."""
from types import SimpleNamespace

import pytest

from hermes_cli import active_sessions, host_loop_lease
from tests.tui_gateway import test_automatic_ledger_defer as ledger_tests
from tests.tui_gateway import test_update_marker_defer as marker_tests
from tests.tui_gateway import test_update_hold_submit_queue as queue_tests
from tui_gateway import server, update_marker_owner

held = queue_tests.held

admission = ledger_tests.admission
custody = marker_tests.custody


@pytest.mark.parametrize('automatic', [False, True])
def test_updating_owner_never_publishes_a_card(custody, admission, automatic):
    admission.session['_synthesized_turn'] = automatic
    admission.refusal = custody.classify()
    assert ledger_tests._admit(admission) is None
    assert admission.errors == []
    assert admission.starts == []


@pytest.mark.parametrize('automatic', [False, True])
def test_unknown_owner_never_publishes_a_card(admission, automatic, tmp_path):
    refusal = active_sessions.ActiveSessionRefusal('owned', 'SESSION_NOT_OWNED')
    refusal.ownership_cause, refusal.holder_pid = 'lease_foreign', 0
    admission.refusal = update_marker_owner.classify_refusal(tmp_path, 'chat', refusal)
    admission.session['_synthesized_turn'] = automatic
    assert ledger_tests._admit(admission) is None
    assert admission.errors == []
    assert admission.starts == []


def test_same_install_retirement_needs_no_chat_pause_marker(custody, monkeypatch):
    from tui_gateway import turn_marker
    turn_marker.clear_turn_marker(custody.home, 'chat')
    custody.lock.release()
    monkeypatch.setattr(host_loop_lease, 'retiring_reason', lambda pid, home=None: 'superseded')
    assert custody.classify().ownership_cause == 'lease_retiring'


def test_remote_unknown_pid_is_not_local_update(tmp_path):
    refusal = active_sessions.ActiveSessionRefusal('remote owner', 'SESSION_NOT_OWNED')
    refusal.holder_pid, refusal.holder_remote = 0, True
    assert update_marker_owner.classify_refusal(tmp_path, 'chat', refusal) is refusal


@pytest.mark.parametrize('cause', ['update_marker', 'lease_retiring', 'lease_unknown'])
def test_typed_slot_refusal_queues_instead_of_auto_takeover(admission, monkeypatch, cause):
    calls = []
    refusal = active_sessions.ActiveSessionRefusal('owned', 'SESSION_NOT_OWNED')
    refusal.ownership_cause = cause
    monkeypatch.setattr(server, '_queue_submit_behind_update_hold',
                        lambda *a, **kw: calls.append((a, kw)) or {'result': {'status': 'queued'}})
    monkeypatch.setattr(server, '_automation_holder_kind', lambda _: pytest.fail('must not take over'))
    result = server._slot_refusal('rid', 'live', admission.session, 'typed', {}, refusal, False)
    assert result == {'result': {'status': 'queued'}}
    assert calls[0][1]['ownership_cause'] == cause


@pytest.mark.parametrize('cause', ['update_marker', 'lease_unknown', 'lease_retiring'])
def test_real_submit_keeps_queue_until_holder_clears(held, monkeypatch, cause):
    from hermes_cli import admission_fence
    admission_fence.clear_hold(held.home)
    refusal = active_sessions.ActiveSessionRefusal('owned', 'SESSION_NOT_OWNED')
    refusal.ownership_cause = cause
    state = [refusal]
    monkeypatch.setattr(server, '_ensure_active_session_slot', lambda *a: state[0])
    reply = queue_tests._submit()
    assert reply['result']['status'] == 'queued'
    assert held.persisted == [('hello there', None)]
    head = held.session['queued_prompt']
    for _ in range(2):
        held.timers.pop().target()
        assert held.session['queued_prompt'] is head
        assert not held.dispatched
    state[0] = None
    held.timers.pop().target()
    assert held.dispatched == ['hello there']
    assert not held.session.get('queued_prompt')


def test_drain_rechecks_fence_before_consuming_queue(admission, monkeypatch):
    from contextlib import nullcontext
    head = {'text': 'typed'}
    admission.session.update(running=False, queued_prompt=head)
    admission.refusal = SimpleNamespace(reason='SESSION_NOT_OWNED', ownership_cause='lease_unknown')
    monkeypatch.setattr(server, '_session_turn_admission', lambda _: nullcontext(True))
    retries = []
    from tui_gateway import admission_retry
    monkeypatch.setattr(admission_retry, 'retry_queued_prompt', lambda *a: retries.append(a) or True)
    assert server._drain_queued_prompt('rid', 'live', admission.session)
    assert admission.session['queued_prompt'] is head
    assert not admission.session['running']
    assert len(retries) == 1
