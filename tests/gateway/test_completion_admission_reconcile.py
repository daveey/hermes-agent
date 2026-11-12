"""An acknowledged completion whose admission failed is offered again and delivered once (deleg_e757744d).

Before the lease wait (``test_completion_note_turn_lease``), a note refused by another turn's lease
settled its admission ``failed`` while the ledger row already said ``delivered``. The start-up
reconciliation reopens such rows through ``requeue_admitted_completion``; the claimed offer then
writes the failed admission's note, keyed by that admission. Negative controls: an admission that
completed, or a failed one whose note exists, is never reopened.
"""
import logging
import queue

import pytest

from tests.gateway.test_completion_admission import pending
from tests.gateway.test_session_end_delegations import _admissions, _local
from tools import async_delegation as delegation


@pytest.fixture(autouse=True)
def _no_delegations():
    delegation._reset_for_tests()
    yield
    delegation._reset_for_tests()


@pytest.fixture(autouse=True)
def _completion_is_a_note(monkeypatch):
    # Upstream runs an unviewed final result as the parent's turn (``resumes_parent``,
    # test_unviewed_completion_resumes_parent.py). These cases cover the note path (a cron result, a
    # chat the user closed), so keep the outcome a note.
    import tools.process_registry_notifications as notifications
    monkeypatch.setattr(notifications, 'completion_resumes_parent', lambda evt: False)


def _target(runner, authority, ref):
    return runner.session_store.lookup_by_session_key(authority.sessions[ref.session_id].route).session_id


def _notes(authority, target, name):
    return [m for m in authority.db.get_messages(target) if name in str(m.get('content'))]


def _ledger(delegation_id):
    with delegation._DB_LOCK, delegation._transaction() as conn:
        return conn.execute('SELECT delivery_state, delivery_attempts FROM async_delegations '
                            'WHERE delegation_id=?', (delegation_id,)).fetchone()


async def _admit(runner, authority, ref, name):
    route = authority.sessions[ref.session_id].route
    evt = dict(pending(route, name), parent_session_id=_target(runner, authority, ref))
    assert await runner._deliver_async_delegation_group([evt]) is True
    assert _ledger(name)[0] == 'delivered', 'acknowledged at admission'
    return evt


async def _fail_next_note(authority, ref, monkeypatch):
    """Drain the queued completion the way the old code did when the note met another turn's lease."""
    from gateway import session_completion_notes as notes
    from hermes_state_errors import SessionTurnLeaseLostError

    async def refused(*_args):
        raise SessionTurnLeaseLostError('Session has an active turn lease; refusing transcript mutation')
    with monkeypatch.context() as patch:
        patch.setattr(notes, '_write_note', refused)
        await authority._drain(ref)


def _offered(q):
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    return events


@pytest.mark.asyncio
async def test_failed_admission_is_reopened_and_noted_exactly_once(tmp_path, monkeypatch):
    from gateway.completion_admission_recovery import reconcile_failed_completion_admissions
    runner, authority, ref, _actor = await _local(tmp_path, monkeypatch)
    try:
        target = _target(runner, authority, ref)
        await _admit(runner, authority, ref, 'deleg_lost')
        await _fail_next_note(authority, ref, monkeypatch)
        assert [r['outcome'] for r in _admissions(authority, ref)] == ['failed']
        assert _notes(authority, target, 'deleg_lost') == [] and _ledger('deleg_lost')[0] == 'delivered'

        # Negative control: a completion whose admission ran its note is never reopened.
        await _admit(runner, authority, ref, 'deleg_fine')
        await authority._drain(ref)
        assert len(_notes(authority, target, 'deleg_fine')) == 1

        offers = queue.Queue()
        assert reconcile_failed_completion_admissions(authority, offers) == 1
        events = _offered(offers)
        assert [e['delegation_id'] for e in events] == ['deleg_lost']
        assert _ledger('deleg_lost')[0] == 'pending' and _ledger('deleg_fine')[0] == 'delivered'

        # The claimed offer writes the failed admission's note; the ledger is acknowledged again.
        assert await runner._deliver_async_delegation_group(events) is True
        assert len(_notes(authority, target, 'deleg_lost')) == 1
        state, attempts = _ledger('deleg_lost')
        assert state == 'delivered' and attempts == 2
        assert [r['outcome'] for r in _admissions(authority, ref)] == ['failed', 'completed']

        # Negative control: once noted, a later start reopens nothing, and a stray copy writes nothing.
        assert reconcile_failed_completion_admissions(authority, offers) == 0
        assert _offered(offers) == []
        assert await runner._deliver_async_delegation_group([dict(events[0])]) in (True, None)
        assert len(_notes(authority, target, 'deleg_lost')) == 1
        assert len(_notes(authority, target, 'deleg_fine')) == 1
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_refused_recovery_note_spends_attempts_until_dropped(tmp_path, monkeypatch):
    """A recovery note still refused (a lease that never clears) releases its claim: the row converges to
    ``dropped`` within the delivery budget, never back to a silent ``delivered``."""
    from gateway.completion_admission_recovery import reconcile_failed_completion_admissions
    runner, authority, ref, _actor = await _local(tmp_path, monkeypatch)
    try:
        target = _target(runner, authority, ref)
        await _admit(runner, authority, ref, 'deleg_held')
        await _fail_next_note(authority, ref, monkeypatch)
        offers = queue.Queue()
        assert reconcile_failed_completion_admissions(authority, offers) == 1
        assert authority.db.try_acquire_session_turn_lease(target, 'other-turn', ttl_seconds=300.0)
        monkeypatch.setattr(delegation, '_CLAIM_LEASE_S', -1.0)
        events = _offered(offers)
        for _ in range(delegation._MAX_DELIVERY_ATTEMPTS):
            assert await runner._deliver_async_delegation_group([dict(events[0])]) is not True
            if _ledger('deleg_held')[0] != 'pending':
                break
        state, attempts = _ledger('deleg_held')
        assert state == 'dropped' and attempts >= 1
        assert _notes(authority, target, 'deleg_held') == []
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_reopen_past_the_replay_age_drops_it_with_a_logged_reason(tmp_path, monkeypatch, caplog):
    from gateway.completion_admission_recovery import reconcile_failed_completion_admissions
    runner, authority, ref, _actor = await _local(tmp_path, monkeypatch)
    try:
        await _admit(runner, authority, ref, 'deleg_old')
        await _fail_next_note(authority, ref, monkeypatch)
        with delegation._DB_LOCK, delegation._transaction() as conn:
            conn.execute('UPDATE async_delegations SET completed_at=?, dispatched_at=? WHERE delegation_id=?',
                         (1.0, 1.0, 'deleg_old'))
        offers = queue.Queue()
        with caplog.at_level(logging.WARNING, logger='tools.async_delegation'):
            assert reconcile_failed_completion_admissions(authority, offers) == 0
        assert _offered(offers) == []
        assert _ledger('deleg_old')[0] == 'dropped'
        assert any('deleg_old' in r.getMessage() and 'replay age' in r.getMessage() for r in caplog.records)
    finally:
        authority.db.close()
