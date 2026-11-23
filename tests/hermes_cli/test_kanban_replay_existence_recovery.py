"""Bounded recovery timing and the supported owner retry CLI. [daveey]"""
from argparse import ArgumentParser
from pathlib import Path

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_replay_existence as existence
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers
from tests.hermes_cli import test_kanban_replay_synthetic_deferred as synthetic
from tests.hermes_cli import test_kanban_replay_current_row as current
from tests.hermes_cli import test_kanban_replay_existence as cases

live = helpers.live


@pytest.mark.parametrize('recover', [False, True])
def test_unavailable_read_uses_eight_bounded_attempts_not_elapsed_offline_age(live, monkeypatch, caplog, recover):
    _tid, receiver = current.issued_parent(live)
    frozen = journal.pending_wire(live)
    now = [1000.0]
    monkeypatch.setattr(existence, 'time', lambda: now[0])
    original = receiver.row_evidence
    reads = []
    def unavailable(*args):
        reads.append(1)
        raise OSError('current service unavailable')
    receiver.row_evidence = unavailable
    for attempt in range(1, 9):
        cases.hold(live, receiver)
        fault = existence.fault(live)
        assert fault['attempts'] == attempt and len(reads) == attempt
        assert fault['retry_after'] - now[0] == min(60, 2 ** attempt)
        assert bool(fault['paused']) == (attempt == 8)
        calls = list(receiver.calls)
        cases.hold(live, receiver)
        assert receiver.calls == calls and len(reads) == attempt
        assert journal.pending_wire(live) == frozen and receiver.slot['a'] == '0'
        if recover and attempt == 2:
            now[0] = fault['retry_after']
            receiver.row_evidence = original
            assert cases.push(live, receiver).applied == 1
            assert existence.fault(live) is None and len(receiver.sent) == 1
            return
        now[0] = fault['retry_after'] + 86400  # offline duration adds no attempt/age
    cases.hold(live, receiver)
    assert len(reads) == 8 and journal.pending_wire(live) == frozen
    assert sum('Replay current-row hold' in r.message for r in caplog.records) == 1


def test_owner_cli_resumes_only_expected_custody_without_network_or_skip(live, monkeypatch, capsys):
    from hermes_cli import kanban_sync_cli as cli
    _tid, receiver = current.issued_parent(live)
    receiver.evidence.update(state='unknown', row=None)
    cases.hold(live, receiver)
    fault = existence.fault(live)
    frozen = journal.pending_wire(live)
    calls = list(receiver.calls)
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    monkeypatch.setattr(cli, '_path', lambda board: path)
    from hermes_cli.kanban_parser import build_parser
    parser = ArgumentParser()
    build_parser(parser.add_subparsers())
    args = parser.parse_args(['kanban', '--board', 'default', 'sync', 'replay-resume',
                              '--expect', 'wrong', '--reason', 'reviewed', '--json'])
    assert cli.cmd_sync(args) == 2 and existence.fault(live) == fault
    args.expect = fault['op_id']
    assert cli.cmd_sync(args) == 0
    assert 'retry_exact' in capsys.readouterr().out
    assert existence.fault(live) is None and journal.pending_wire(live) == frozen
    assert receiver.calls == calls and receiver.slot['a'] == '0'


def test_fault_resume_rejects_changed_generation(live):
    _tid, receiver = current.issued_parent(live)
    receiver.evidence.update(state='unknown', row=None)
    cases.hold(live, receiver)
    fault = existence.fault(live)
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_replay_existence_fault1 SET generation='999'")
    with pytest.raises(ValueError, match='identity changed'):
        existence.resume(live, fault['op_id'], 'reviewed')
    assert existence.fault(live)['generation'] == '999'
    with kbc.write_txn(live):
        existence.clear_settled(live)
    assert existence.fault(live) is not None


def test_primary_live_deferred_recovery_does_not_claim_status_applied(live):
    tid, receiver = synthetic.issue(live)
    assert cases.push(live, receiver, primary=True).deferred == 1
    assert receiver.slot['a'] == '1' and len(receiver.sent) == 1
    assert journal.pending_wire(live) is None
    assert live.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 19


def test_stale_read_error_cannot_pause_successor_custody(live):
    _tid, _receiver = current.issued_parent(live)
    frozen = journal.pending_wire(live)
    error = existence.CurrentRowHold('earlier read failed')
    error.expected = ('another-journal', '1', '1', 'another-op')
    with pytest.raises(client.SyncUnavailable, match='superseded'):
        existence.pause(live, 'default', error)
    assert existence.fault(live) is None and journal.pending_wire(live) == frozen
