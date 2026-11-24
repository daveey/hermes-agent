"""Independent exact-head review regression: overlapping recovery episodes."""
from argparse import ArgumentParser
from pathlib import Path
import pytest
from hermes_cli import kanban_replay_existence as existence
from hermes_cli import kanban_replay_recovery as recovery
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_cli as cli
from hermes_cli import kanban_sync_client as client
from hermes_cli.kanban_parser import build_parser
from tests.hermes_cli import test_kanban_replay_sender as helpers
from tests.hermes_cli import test_kanban_replay_current_row as current
live = helpers.live

@pytest.mark.parametrize('refusal', ['none', 'wrong_expect', 'transport_identity', 'row_identity', 'no_reason',
                                   'existence_only', 'transport_only', 'no_fault'])
def test_resume_clears_same_identity_overlapping_faults(live, monkeypatch, refusal):
    now = [1000.0]
    monkeypatch.setattr(existence, 'time', lambda: now[0])
    monkeypatch.setattr(recovery, 'time', lambda: now[0])
    _, receiver = current.issued_parent(live)
    original_row = receiver.row_evidence
    def unavailable(*args):
        raise OSError('current row unavailable')
    receiver.row_evidence = unavailable
    with pytest.raises(existence.CurrentRowHold):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    row_fault = existence.fault(live)
    assert row_fault and not row_fault['paused']
    now[0] = row_fault['retry_after']
    original_call = receiver._call
    def broken_inspect(method, path, body=None):
        if path.endswith('/inspect'):
            raise OSError('inspection unavailable')
        return original_call(method, path, body)
    receiver._call = broken_inspect
    held = None
    for _ in range(8):
        with pytest.raises(OSError):
            client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
        held = recovery.fault(live)
        assert held is not None
        now[0] = held['retry_after']
        if held['paused']:
            break
    assert held is not None and held['paused'] and held['op_id'] == row_fault['op_id']
    frozen = journal.pending_wire(live)
    receiver._call = original_call
    receiver.row_evidence = original_row
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    monkeypatch.setattr(cli, '_path', lambda board: path)
    parser = ArgumentParser()
    build_parser(parser.add_subparsers())
    args = parser.parse_args(['kanban', '--board', 'default', 'sync', 'replay-resume',
                             '--expect', held['op_id'], '--reason', 'both services repaired', '--json'])
    if refusal in ('existence_only', 'no_fault'):
        live.execute('DELETE FROM kanban_replay_transport_fault1')
    if refusal in ('transport_only', 'no_fault'):
        live.execute('DELETE FROM kanban_replay_existence_fault1')
    live.commit()
    if refusal == 'no_fault':
        assert cli.cmd_sync(args) == 2
        assert journal.pending_wire(live) == frozen
        return
    if refusal not in ('none', 'existence_only', 'transport_only'):
        table = None
        if refusal == 'wrong_expect':
            args.expect = 'different-operation'
        elif refusal == 'no_reason':
            args.reason = ' '
        else:
            table = ('kanban_replay_transport_fault1' if refusal == 'transport_identity'
                     else 'kanban_replay_existence_fault1')
            live.execute(f"UPDATE {table} SET generation='different-generation'")
            live.commit()
        row_before, transport_before = existence.fault(live), recovery.fault(live)
        assert cli.cmd_sync(args) == 2
        assert existence.fault(live) == row_before
        assert recovery.fault(live) == transport_before
        assert journal.pending_wire(live) == frozen
        if refusal in ('transport_identity', 'row_identity'):
            assert table is not None
            live.execute(f'UPDATE {table} SET generation=?', (held['generation'],))
            live.commit()
        args.expect, args.reason = held['op_id'], 'both services repaired'
    assert cli.cmd_sync(args) == 0
    assert journal.pending_wire(live) == frozen
    assert existence.fault(live) is None
    assert recovery.fault(live) is None
    # Successful explicit same-identity resume must permit the repaired sender.
    result = client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert result.applied == 1
