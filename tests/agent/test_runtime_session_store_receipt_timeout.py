"""One slow owner reply must not kill a managed worker, and must never commit a write twice.

A ``worker.persist`` with no reply inside the receipt budget (``worker_receipt_timeout``) or a closed
owner link leaves the outcome unknown, but the owner resolves an exact resend of the same
(execution, sequence, digest) from its receipt row. So the store re-sends the pending entry, never
renumbered, INSIDE the same call, and the caller gets the real result (mini 11-12 20:39 PT: one stall
latched the store and the next lease refresh stopped a bb-dev worker's turn). The resend is never
deferred to a later write: a transcript caller that saw the exception re-collects the same rows, so a
deferred resend would commit them twice (PR #2490 review). Only a bounded run of unknown outcomes in
one call, or any other failure, stays sticky.
"""
import logging
import time
from types import SimpleNamespace

import pytest

from agent import runtime_session_store as store_module
from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError, is_retryable_owner_error
from agent.turn_facade_lease import LEASE_TTL_SECONDS, DurableTurnLease


class FakeOwner:
    """Commits each sequence once; an exact resend returns the stored result (the receipt row)."""

    def __init__(self):
        self.commits = []
        self.rows = []  # transcript rows in commit order
        self.receipts = {}
        self.sends = 0
        self.stalls = []  # queued: 'after_commit' | 'before_commit' | 'closed'

    def __call__(self, method, **params):
        assert method == 'worker.persist'
        self.sends += 1
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
            if params['operation'] == 'transcript.append':
                messages = params['payload']['messages']
                self.rows.extend(message['content'] for message in messages)
                result = {'count': len(messages), 'annotations': [{'stored': True} for _ in messages]}
            else:
                result = {'value': True}
            self.receipts[sequence] = result
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
def test_one_stalled_reply_is_resent_inside_the_same_call(owner_store, stall, caplog):
    owner, store = owner_store
    owner.stalls.append(stall)
    caplog.set_level(logging.WARNING, logger='agent.runtime_session_store')
    store.queue_token_counts('s', input_tokens=1)
    assert owner.sends == 2, 'the stalled entry is re-sent once, by the same call'
    assert owner.commits == [(1, 'usage.main')]
    assert store.journal['pending'] == [] and store.failure is None
    store.queue_token_counts('s', input_tokens=2)
    assert owner.commits == [(1, 'usage.main'), (2, 'usage.main')], 'the next write takes the next sequence'
    recovered = [r.getMessage() for r in caplog.records if 'worker receipt outcome recovered after' in r.getMessage()]
    assert len(recovered) == 1 and 'op=usage.main' in recovered[0] and 'sends=2' in recovered[0], \
        'only the stalled write logs a recovery'


def test_a_stalled_transcript_append_returns_its_real_result_and_commits_once(owner_store):
    """Review of PR #2490: a deferred resend plus the caller's re-flush committed rows A twice."""
    owner, store = owner_store
    owner.stalls.append('after_commit')
    rows = [{'role': 'user', 'content': 'A'}]
    assert store.append_messages_batch('s', rows) == 1
    assert rows[0]['stored'] is True, 'the resend result (row annotations) reaches the caller'
    assert store.append_messages_batch('s', [{'role': 'user', 'content': 'B'}]) == 1
    assert owner.rows == ['A', 'B']


def test_an_exhausted_transcript_append_latches_and_never_duplicates_on_reflush(owner_store):
    owner, store = owner_store
    owner.stalls.extend(['after_commit'] * store_module._RETRYABLE_ATTEMPTS)
    with pytest.raises(WorkerPersistenceError, match='worker_receipt_timeout'):
        store.append_messages_batch('s', [{'role': 'user', 'content': 'A'}])
    assert owner.sends == store_module._RETRYABLE_ATTEMPTS
    # The caller saw the failure and re-flushes the same unmarked rows plus a new one.
    sends = owner.sends
    with pytest.raises(WorkerPersistenceError, match='worker_receipt_timeout'):
        store.append_messages_batch('s', [{'role': 'user', 'content': 'A'}, {'role': 'user', 'content': 'B'}])
    assert owner.sends == sends, 'a latched store sends nothing'
    assert owner.rows == ['A']


def test_close_flush_after_a_recovered_stall(owner_store):
    owner, store = owner_store
    owner.stalls.append('before_commit')
    store.queue_token_counts('s', input_tokens=1)
    assert store.flush_token_counts() is True
    assert owner.commits == [(1, 'usage.main')]


def _lease(store, interrupts, *, deadline_in=LEASE_TTL_SECONDS):
    agent = SimpleNamespace(session_id='s', interrupt=lambda message, **kwargs: interrupts.append(message))
    lease = DurableTurnLease(agent, store, 's', 'holder', expires_at=time.time() + deadline_in)
    lease.turn_active = True
    return lease


def test_refresh_whose_reply_times_out_once_keeps_the_turn(owner_store):
    owner, store = owner_store
    interrupts = []
    lease = _lease(store, interrupts)
    before = lease._authority_deadline
    owner.stalls.append('before_commit')
    assert lease.refresh_tick() is None
    assert interrupts == [] and lease.interrupt_message is None
    assert owner.commits == [(1, 'turn.renew')] and owner.sends == 2
    assert lease._authority_deadline >= before


def test_unknown_outcomes_are_bounded_per_call(owner_store, caplog):
    owner, store = owner_store
    owner.stalls.extend(['before_commit'] * 10)
    caplog.set_level(logging.ERROR, logger='agent.runtime_session_store')
    with pytest.raises(WorkerPersistenceError, match='worker_receipt_timeout'):
        store.queue_token_counts('s', input_tokens=1)
    assert owner.sends == store_module._RETRYABLE_ATTEMPTS == 2
    assert any('worker receipt outcome still unknown after 2 sends' in r.getMessage() for r in caplog.records)
    owner.stalls.clear()
    with pytest.raises(WorkerPersistenceError):
        store.queue_token_counts('s', input_tokens=2)
    assert owner.sends == 2 and owner.commits == [], 'an exhausted latch sends nothing'


def test_an_owner_refusal_is_not_resent(owner_store):
    owner, store = owner_store
    store.journal['next_sequence'] = 5  # the owner expects 1 and the journal holds no acked entry
    with pytest.raises(WorkerPersistenceError, match='lost_receipt'):
        store.queue_token_counts('s', input_tokens=1)
    assert owner.sends == 1


def test_a_timeout_other_than_the_receipt_budget_is_not_resent(owner_store, monkeypatch):
    owner, store = owner_store

    def stall(**entry):
        owner.sends += 1
        raise TimeoutError('socket read timed out')

    monkeypatch.setattr(store, '_persist', lambda entry: stall(**entry))
    with pytest.raises(TimeoutError, match='socket read'):
        store.queue_token_counts('s', input_tokens=1)
    assert owner.sends == 1


def test_retryable_classification():
    from websockets.exceptions import ConnectionClosedError

    assert is_retryable_owner_error(TimeoutError('worker_receipt_timeout'))
    assert not is_retryable_owner_error(TimeoutError('other'))
    assert is_retryable_owner_error(ConnectionResetError())
    assert is_retryable_owner_error(ConnectionClosedError(None, None))
    assert not is_retryable_owner_error(WorkerPersistenceError('worker_receipt_timeout'))
    assert not is_retryable_owner_error(ValueError('worker_receipt_timeout'))


def _closed(rcvd=None, sent=None):
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close
    rcvd = Close(rcvd, 'owner text that varies') if rcvd else None
    sent = Close(sent, 'ours') if sent else None
    return ConnectionClosedError(rcvd, sent, True if rcvd and sent else None)


@pytest.mark.parametrize('exc, code', [
    (TimeoutError('worker_receipt_timeout'), 'worker_receipt_timeout'),
    (ConnectionResetError(104, 'Connection reset by peer'), 'owner_link_closed'),
    (ConnectionRefusedError(111, 'Connection refused'), 'owner_link_closed'),
    (_closed(), 'owner_link_closed'),  # socket dropped, no close frame
    (_closed(rcvd=1001, sent=1001), 'owner_link_closed'),  # owner going away
    (_closed(rcvd=1011), 'owner_link_closed'),  # owner internal error
    (_closed(rcvd=1012, sent=1012), 'owner_link_closed'),  # owner restarting
])
def test_exhausted_resend_latches_a_fixed_owner_code(exc, code):
    assert store_module.exhausted_resend_code(exc) == code


@pytest.mark.parametrize('exc', [
    _closed(rcvd=1009, sent=1009),  # the owner refused THIS frame's size
    _closed(rcvd=1008),  # policy violation
    _closed(sent=1009),  # this side failed the link on an oversized reply
    _closed(rcvd=1000, sent=1000),
])
def test_a_close_that_may_answer_the_frame_keeps_its_text(exc):
    assert store_module.exhausted_resend_code(exc) == str(exc)


@pytest.mark.parametrize('stall, code', [('closed', 'owner_link_closed'), ('before_commit', 'worker_receipt_timeout')])
def test_exhausted_resend_terminal_envelope_is_exact(owner_store, stall, code):
    """The bound worker's terminal line is the whole-line envelope the dispatcher books as an owner fault."""
    from gateway.session_kanban import failure_line
    from hermes_cli import kanban_owner_storage_refusal as refusal
    owner, store = owner_store
    owner.stalls.extend([stall] * store_module._RETRYABLE_ATTEMPTS)
    with pytest.raises(WorkerPersistenceError) as raised:
        store.queue_token_counts('s', input_tokens=1)
    assert str(raised.value) == code and store.failure == code
    line = failure_line(raised.value, 'Worker failed')
    assert line == f'Worker failed: WorkerPersistenceError: {code}'
    assert refusal._proof({'exit_code': 1, 'last_output': line})['kind'] == 'owner_unreachable'
    with pytest.raises(WorkerPersistenceError) as latched:  # a later write re-raises the same code
        store.queue_token_counts('s', input_tokens=2)
    assert failure_line(latched.value, 'Worker failed') == line


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
