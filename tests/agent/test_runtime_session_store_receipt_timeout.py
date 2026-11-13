"""One slow owner reply must not kill a managed worker.

A ``worker.persist`` with no reply inside the receipt budget (``worker_receipt_timeout``) or a closed
owner link leaves the outcome unknown, but the owner resolves an exact resend of the same
(execution, sequence, digest) from its receipt row. So the store keeps the pending entry and the
next write (or turn-lease refresh) re-sends it, never renumbered; only a bounded run of such
failures, or any other failure, stays sticky (mini 11-12 20:39 PT: one stall latched the store and
the next lease refresh stopped a bb-dev worker's turn).
"""
import logging
import time
from types import SimpleNamespace

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError
from agent.turn_facade_lease import LEASE_TTL_SECONDS, DurableTurnLease


class FakeOwner:
    """Commits each sequence once; an exact resend returns the stored result (the receipt row)."""

    def __init__(self):
        self.commits = []
        self.receipts = {}
        self.stalls = []  # queued: 'after_commit' | 'before_commit' | 'closed'

    def __call__(self, method, **params):
        assert method == 'worker.persist'
        sequence = params['sequence']
        stall = self.stalls.pop(0) if self.stalls else None
        if stall == 'closed':
            raise ConnectionResetError('owner link closed')
        if stall == 'before_commit':
            raise TimeoutError('worker_receipt_timeout')
        if sequence in self.receipts:
            result = self.receipts[sequence]
        else:
            expected = len(self.commits) + 1
            if sequence != expected:
                failure = WorkerPersistenceError('invalid_params')
                failure.data = {'field': 'sequence', 'expected_sequence': expected}
                raise failure
            self.commits.append((sequence, params['operation']))
            result = self.receipts[sequence] = {'value': True}
        if stall == 'after_commit':
            raise TimeoutError('worker_receipt_timeout')
        return dict(result)


@pytest.fixture
def owner_store(tmp_path):
    owner = FakeOwner()
    store = RuntimeSessionStore(owner, {'session_id': 's'}, tmp_path / 'private')
    yield owner, store
    store.failure = None
    store.journal['pending'] = []
    store.close()


@pytest.mark.parametrize('stall', ['after_commit', 'before_commit', 'closed'])
def test_one_stalled_reply_then_next_write_lands_once_without_a_gap(owner_store, stall, caplog):
    owner, store = owner_store
    owner.stalls.append(stall)
    with pytest.raises((TimeoutError, ConnectionError)):
        store.queue_token_counts('s', input_tokens=1)
    caplog.set_level(logging.WARNING, logger='agent.runtime_session_store')
    store.queue_token_counts('s', input_tokens=2)
    assert owner.commits == [(1, 'usage.main'), (2, 'usage.main')]
    assert store.journal['pending'] == [] and store.failure is None
    assert any('worker receipt timeout recovered after' in r.getMessage() and 'op=usage.main' in r.getMessage()
               for r in caplog.records)


def test_close_flush_resends_a_timed_out_entry(owner_store):
    owner, store = owner_store
    owner.stalls.append('before_commit')
    with pytest.raises(TimeoutError):
        store.queue_token_counts('s', input_tokens=1)
    assert store.flush_token_counts() is True
    assert owner.commits == [(1, 'usage.main')]


def _lease(store, interrupts, *, deadline_in=LEASE_TTL_SECONDS):
    agent = SimpleNamespace(session_id='s', interrupt=lambda message, **kwargs: interrupts.append(message))
    lease = DurableTurnLease(agent, store, 's', 'holder', expires_at=time.time() + deadline_in)
    lease.turn_active = True
    return lease


def test_refresh_after_an_earlier_write_timed_out_keeps_the_turn(owner_store):
    """The incident: an earlier write latched the store, then the refresh raised the latch."""
    owner, store = owner_store
    owner.stalls.append('after_commit')
    with pytest.raises(TimeoutError):
        store.queue_token_counts('s', input_tokens=1)
    interrupts = []
    lease = _lease(store, interrupts)
    before = lease._authority_deadline
    assert lease.refresh_tick() is None
    assert interrupts == [] and lease.interrupt_message is None
    assert owner.commits == [(1, 'usage.main'), (2, 'turn.renew')]
    assert lease._authority_deadline >= before


def test_refresh_whose_own_reply_times_out_retries_next_tick(owner_store):
    owner, store = owner_store
    interrupts = []
    lease = _lease(store, interrupts)
    owner.stalls.append('before_commit')
    assert lease.refresh_tick() is None, 'a retryable owner stall is a missed tick, not a lost lease'
    assert interrupts == []
    before = lease._authority_deadline
    assert lease.refresh_tick() is None
    assert interrupts == [] and owner.commits == [(1, 'turn.renew'), (2, 'turn.renew')], \
        'the stalled renewal is re-sent under its own sequence, then this tick renews'
    assert lease._authority_deadline >= before


def test_refresh_timeout_near_the_deadline_still_stops(owner_store):
    owner, store = owner_store
    interrupts = []
    # One refresh interval (60 s default) plus less than the 2 s expiry margin: the next tick is too late.
    lease = _lease(store, interrupts, deadline_in=61.0)
    owner.stalls.append('before_commit')
    assert lease.refresh_tick() is False
    assert interrupts, 'no renewal can land before expiry: the turn must stop'


def test_retryable_failures_are_bounded(owner_store):
    owner, store = owner_store
    owner.stalls.extend(['before_commit'] * 10)
    raised = 0
    for _ in range(10):
        try:
            store.queue_token_counts('s', input_tokens=1)
        except WorkerPersistenceError:
            break
        except TimeoutError:
            raised += 1
    else:
        pytest.fail('a run of receipt timeouts never latched the store')
    assert 1 <= raised <= 4
    owner.stalls.clear()
    calls = len(owner.commits)
    with pytest.raises(WorkerPersistenceError):
        store.queue_token_counts('s', input_tokens=2)
    assert len(owner.commits) == calls, 'an exhausted latch sends nothing'


def test_lost_receipt_stays_sticky(owner_store):
    owner, store = owner_store
    owner.commits.append((0, 'phantom'))  # the owner expects 2; the journal holds no acked entry 1
    owner.receipts.clear()
    store.journal['next_sequence'] = 3
    with pytest.raises(WorkerPersistenceError, match='lost_receipt'):
        store.queue_token_counts('s', input_tokens=1)
    sent = list(owner.commits)
    with pytest.raises(WorkerPersistenceError, match='lost_receipt'):
        store.queue_token_counts('s', input_tokens=2)
    assert owner.commits == sent


def test_outbox_full_stays_sticky(tmp_path):
    owner = FakeOwner()
    store = RuntimeSessionStore(owner, {'session_id': 's'}, tmp_path / 'private', max_bytes=400)
    try:
        with pytest.raises(WorkerPersistenceError, match='outbox_full'):
            store.queue_token_counts('s', blob='x' * 1000)
        with pytest.raises(WorkerPersistenceError, match='outbox_full'):
            store.queue_token_counts('s', input_tokens=1)
        assert owner.commits == []
    finally:
        store.failure = None
        store.close()
