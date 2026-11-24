"""Time-bound, delivery dedup and transaction controls for strict recovery."""
import logging
from argparse import ArgumentParser
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_replay_recovery as recovery
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_transport_recovery as helpers

live = helpers.live


def test_five_reachable_minutes_stop_before_eight_attempts(live, monkeypatch, caplog):
    now = [1000.0]
    monkeypatch.setattr(recovery, 'time', lambda: now[0])
    kb.create_task(live, title='reachable budget', assignee='worker')

    class Slow(helpers.Unavailable):
        def _call(self, method, path, body=None):
            if path.endswith('/ops'):
                now[0] += 150
            return super()._call(method, path, body)

    peer = Slow()
    with caplog.at_level(logging.ERROR):
        for attempt in range(2):
            with pytest.raises(OSError):
                helpers.push(live, peer)
            held = recovery.fault(live)
            assert held['attempts'] == attempt + 1
            assert held['reachable_s'] == (attempt + 1) * 150
            now[0] = held['retry_after']
        assert held['paused'] == 1
        assert len([r for r in caplog.records if r.name == recovery.__name__]) == 1
        before = list(peer.calls)
        for _ in range(3):
            with pytest.raises(client.SyncUnavailable, match='replay-resume'):
                helpers.push(live, peer)
        assert peer.calls == before
        assert recovery.fault(live) == held
        assert len([r for r in caplog.records if r.name == recovery.__name__]) == 1


@pytest.mark.parametrize('detail', ['stream_gap', 'stream_ack_required'])
def test_server_recovery_refusal_inspects_before_backoff(live, detail):
    kb.create_task(live, title='protocol recovery', assignee='worker')

    class Refusal(helpers.Unavailable):
        def _call(self, method, path, body=None):
            if path.endswith('/ops'):
                self.calls.append('ops')
                raise client.HubError(409, detail)
            return super()._call(method, path, body)

    peer = Refusal()
    with pytest.raises(client.HubError, match=detail):
        helpers.push(live, peer)
    assert peer.calls[-2:] == ['ops', 'inspect']
    held = recovery.fault(live)
    assert held['attempts'] == 1 and detail in held['reason']
    assert held['op_id'] == journal.snapshot(live)['pending']['op_id']


def test_resume_rollback_and_changed_identity_preserve_fault(live):
    kb.create_task(live, title='resume custody', assignee='worker')
    with pytest.raises(OSError):
        helpers.push(live, helpers.Unavailable())
    held = recovery.fault(live)
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_replay_transport_fault1 SET journal_id='foreign-journal'")
    with pytest.raises(ValueError, match='identity'):
        recovery.resume(live, held['op_id'], 'checked')
    assert recovery.fault(live)['journal_id'] == 'foreign-journal'
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_replay_transport_fault1 SET journal_id=?', (held['journal_id'],))
    with pytest.raises(RuntimeError, match='transaction'):
        with kbc.write_txn(live):
            recovery.resume(live, held['op_id'], 'caller transaction cannot own recovery')
    assert recovery.fault(live) == held


def test_cli_transport_resume_is_identity_checked_and_has_no_io(live, monkeypatch, capsys):
    from hermes_cli import kanban_sync_cli as cli
    from hermes_cli.kanban_parser import build_parser
    kb.create_task(live, title='CLI transport repair', assignee='worker')
    peer = helpers.Unavailable()
    with pytest.raises(OSError):
        helpers.push(live, peer)
    held = recovery.fault(live)
    frozen = journal.pending_wire(live)
    calls = list(peer.calls)
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    monkeypatch.setattr(cli, '_path', lambda board: path)
    parser = ArgumentParser()
    build_parser(parser.add_subparsers())
    args = parser.parse_args(['kanban', '--board', 'default', 'sync', 'replay-resume',
                              '--expect', 'wrong', '--reason', 'verified repair', '--json'])
    assert cli.cmd_sync(args) == 2 and recovery.fault(live) == held
    args.expect = held['op_id']
    assert cli.cmd_sync(args) == 0 and recovery.fault(live) is None
    assert 'retry_exact' in capsys.readouterr().out
    assert journal.pending_wire(live) == frozen and peer.calls == calls
