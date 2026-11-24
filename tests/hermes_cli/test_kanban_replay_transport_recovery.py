"""Bound automatic strict-stream recovery without dropping issued custody."""
import pytest
from pathlib import Path

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers

live = helpers.live


class Unavailable(helpers.Receiver):
    def __init__(self):
        super().__init__()
        self.offline = False
        self.fail_ops = True

    def _call(self, method, path, body=None):
        action = path.rsplit('/', 1)[-1]
        if self.offline or (action == 'ops' and self.fail_ops):
            self.calls.append(action)
            raise OSError('offline' if self.offline else 'mutation timeout')
        return super()._call(method, path, body)


def push(conn, peer):
    return client.push(conn, peer, 'default', 'macbook', primary=False, max_batches=1)


def test_public_transport_failure_has_durable_status_fault(live):
    kb.create_task(live, title='visible transport recovery', assignee='worker')
    peer = Unavailable()
    with pytest.raises(OSError):
        push(live, peer)
    held = client.status(live).get('replay_transport_fault')
    assert held is not None, 'general mutation timeout has no durable recovery fault'
    assert held['attempts'] == 1 and held['sequence'] == '1'
    assert held['journal_id'] == journal.snapshot(live)['journal_id']
    assert 'replay-resume' in held['recovery']


def test_eight_reachable_attempts_stop_persist_and_require_exact_resume(live, monkeypatch):
    from hermes_cli import kanban_replay_recovery as recovery
    now = [1000.0]
    monkeypatch.setattr(recovery, 'time', lambda: now[0])
    kb.create_task(live, title='bounded recovery', assignee='worker')
    peer = Unavailable()
    for attempt in range(1, 9):
        with pytest.raises(OSError, match='mutation timeout'):
            push(live, peer)
        fault = recovery.fault(live)
        assert fault['attempts'] == attempt
        assert bool(fault['paused']) == (attempt == 8)
        assert fault['retry_after'] - now[0] == min(60, 2 ** attempt)
        assert peer.calls[-1] == 'inspect'
        now[0] = fault['retry_after']
    frozen = journal.pending_wire(live)
    rows = [tuple(row) for row in live.execute('SELECT * FROM kanban_sync_outbox')]
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    with kbc.connect(path) as reopened:
        before = list(peer.calls)
        with pytest.raises(client.SyncUnavailable, match='replay-resume'):
            push(reopened, peer)
        assert peer.calls == before
        assert recovery.fault(reopened) == fault
        assert journal.pending_wire(reopened) == frozen
        assert [tuple(row) for row in reopened.execute('SELECT * FROM kanban_sync_outbox')] == rows
        with pytest.raises(ValueError, match='identity'):
            recovery.resume(reopened, 'wrong-identity', 'checked')
        with pytest.raises(ValueError, match='reason'):
            recovery.resume(reopened, fault['op_id'], ' ')
        assert recovery.fault(reopened) == fault
        recovery.resume(reopened, fault['op_id'], 'transport repaired')
        peer.fail_ops = False
        assert push(reopened, peer).applied == 1
        assert journal.pending_wire(reopened) is None
        assert recovery.fault(reopened) is None


def test_offline_intervals_neither_consume_attempts_nor_age_custody(live, monkeypatch):
    from hermes_cli import kanban_replay_recovery as recovery
    now = [1000.0]
    monkeypatch.setattr(recovery, 'time', lambda: now[0])
    kb.create_task(live, title='offline custody', assignee='worker')
    peer = Unavailable()
    with pytest.raises(OSError):
        push(live, peer)
    fault = recovery.fault(live)
    frozen = journal.pending_wire(live)
    peer.offline = True
    for _ in range(12):
        now[0] += 3600
        with pytest.raises(OSError, match='offline'):
            push(live, peer)
        assert recovery.fault(live) == fault
        assert journal.pending_wire(live) == frozen
    peer.offline = False
    peer.fail_ops = False
    assert push(live, peer).applied == 1
    assert recovery.fault(live) is None


def test_backoff_allows_inspection_but_not_mutation_retry(live, monkeypatch):
    from hermes_cli import kanban_replay_recovery as recovery
    monkeypatch.setattr(recovery, 'time', lambda: 1000.0)
    kb.create_task(live, title='backoff', assignee='worker')
    peer = Unavailable()
    with pytest.raises(OSError):
        push(live, peer)
    calls = list(peer.calls)
    with pytest.raises(client.SyncUnavailable, match='backoff'):
        push(live, peer)
    assert peer.calls[len(calls):] == ['capability', 'inspect', 'session']
    assert recovery.fault(live)['attempts'] == 1
