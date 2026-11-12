"""A completion note refused by another turn's lease waits for it; the result is never lost.

Regression (live, deleg_e757744d): an async delegation's completion was admitted and its ledger row
acknowledged ``delivered`` at admission. When the admission came up with no viewer attached, its
note write met a turn lease another turn held on the same conversation
(``SessionTurnLeaseLostError``); the admission settled ``failed`` and the result never reached the
parent. A request id is admitted once, so a re-offer resolves to that failed admission and cannot
deliver again: the note must land from the admission itself once the lease is gone.
"""
import asyncio

import pytest

from tests.gateway.test_completion_admission import pending
from tests.gateway.test_session_end_delegations import _admissions, _local
from tools import async_delegation as delegation


@pytest.fixture(autouse=True)
def _no_delegations():
    delegation._reset_for_tests()
    yield
    delegation._reset_for_tests()


def _target(runner, authority, ref):
    return runner.session_store.lookup_by_session_key(authority.sessions[ref.session_id].route).session_id


def _delegation_notes(authority, target, name):
    return [m for m in authority.db.get_messages(target) if name in str(m.get('content'))]


def _ledger_state(delegation_id):
    with delegation._DB_LOCK, delegation._transaction() as conn:
        return conn.execute('SELECT delivery_state FROM async_delegations WHERE delegation_id=?',
                            (delegation_id,)).fetchone()[0]


async def _until(predicate, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'condition not reached'
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_note_held_off_by_another_turns_lease_lands_once_it_releases(tmp_path, monkeypatch):
    from gateway import session_completion_notes as notes
    monkeypatch.setattr(notes, '_LEASE_RETRY_MIN_S', 0.01, raising=False)
    monkeypatch.setattr(notes, '_LEASE_RETRY_MAX_S', 0.02, raising=False)
    runner, authority, ref, _actor = await _local(tmp_path, monkeypatch)
    try:
        route = authority.sessions[ref.session_id].route
        target = _target(runner, authority, ref)
        evt = dict(pending(route, 'deleg_leased'), parent_session_id=target)
        assert await runner._deliver_async_delegation_group([evt]) is True
        assert _ledger_state('deleg_leased') == 'delivered'

        # Another turn (another process's, e.g. a Kanban notice) holds the conversation's turn lease.
        assert authority.db.try_acquire_session_turn_lease(target, 'other-turn', ttl_seconds=300.0)
        refused = []
        write = authority.db.append_completion_note

        def counted(*args, **kwargs):
            try:
                return write(*args, **kwargs)
            except Exception as exc:
                refused.append(type(exc).__name__)
                raise
        monkeypatch.setattr(authority.db, 'append_completion_note', counted)

        drain = asyncio.ensure_future(authority._drain(ref))
        await _until(lambda: len(refused) >= 2 or drain.done())
        assert not drain.done(), 'the admission settled while the lease was still held'
        assert set(refused) == {'SessionTurnLeaseLostError'}
        assert _delegation_notes(authority, target, 'deleg_leased') == []

        authority.db.release_session_turn_lease(target, 'other-turn')
        await asyncio.wait_for(drain, 10.0)
        assert len(_delegation_notes(authority, target, 'deleg_leased')) == 1
        assert [r['outcome'] for r in _admissions(authority, ref)] == ['completed']
        assert _ledger_state('deleg_leased') == 'delivered'

        # Exactly once: a re-offer resolves to the settled admission and writes nothing more.
        assert await runner._deliver_async_delegation_group([dict(evt)]) in (True, None)
        await authority._drain(ref)
        assert len(_delegation_notes(authority, target, 'deleg_leased')) == 1
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_note_reclaims_a_dead_holders_lease(tmp_path, monkeypatch):
    """An expired lease (its holder died) is no wait: the guard reclaims it and the note lands at once."""
    from gateway import session_completion_notes as notes
    monkeypatch.setattr(notes, '_LEASE_RETRY_MIN_S', 0.01, raising=False)
    monkeypatch.setattr(notes, '_LEASE_RETRY_MAX_S', 0.02, raising=False)
    runner, authority, ref, _actor = await _local(tmp_path, monkeypatch)
    try:
        route = authority.sessions[ref.session_id].route
        target = _target(runner, authority, ref)
        evt = dict(pending(route, 'deleg_stale'), parent_session_id=target)
        assert await runner._deliver_async_delegation_group([evt]) is True
        assert authority.db.try_acquire_session_turn_lease(target, 'dead-turn', ttl_seconds=300.0)
        authority.db._execute_write(lambda conn: conn.execute(
            "UPDATE session_turn_leases SET expires_at = 0 WHERE holder = 'dead-turn'"))
        await asyncio.wait_for(authority._drain(ref), 10.0)
        assert len(_delegation_notes(authority, target, 'deleg_stale')) == 1
        assert [r['outcome'] for r in _admissions(authority, ref)] == ['completed']
    finally:
        authority.db.close()
