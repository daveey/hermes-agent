"""Real multi-board sync dispatch with a later active strict stream settling.

The feed is an empty fixture feed, not live Board/replica interoperability proof.
No sender, per-board round, journal, settlement or dispatch function is replaced.
"""
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_replay_episodes as episodes
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_source_clones
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as sender
from tests.hermes_cli.test_kanban_replay_journal import binding
from tests.hermes_cli.test_kanban_replay_state_model import PrefixOracle, custody

live = sender.live


@pytest.mark.parametrize('defect', ['capability', 'inspect', 'session', 'transport'])
def test_faulted_stream_does_not_stop_active_healthy_settlement(live, tmp_path, monkeypatch, defect):
    kb.create_task(live, title='faulted stream retained work', assignee='worker')
    broken_path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    healthy_path = tmp_path / 'healthy.db'
    with kbc.connect(healthy_path) as healthy:
        client.ensure_identity(healthy, 'healthy', 'macbook')
        sync.set_mode(healthy, 'shadow')
        with kbc.write_txn(healthy):
            journal.initialize(healthy, binding(), board_id=healthy.execute(
                'SELECT board_id FROM kanban_sync_state').fetchone()[0])
        tid = kb.create_task(healthy, title='healthy strict work', assignee='worker')
        expected = healthy.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0]
    broken, good = PrefixOracle(), PrefixOracle()

    class Multiplex:
        ident = SimpleNamespace(gateway='macbook')
        repaired = False

        def _call(self, method, path, body=None):
            board = unquote(path.split('/')[3])
            peer = broken if board == 'default' else good
            action = path.rsplit('/', 1)[-1]
            if board == 'default' and not self.repaired and defect == 'transport':
                raise OSError('isolated strict transport outage')
            reply = peer._call(method, path, body)
            if board == 'default' and not self.repaired:
                if defect == action == 'capability':
                    reply['processed_prefix'] = False
                elif action == defect:
                    reply['slot']['journal_id'] = 'foreign-journal'
            return reply

        def changes(self, board, since, limit=500):
            assert board in ('default', 'healthy')
            return dict(changes=[], next=since, head=since, more=False)

    hub = Multiplex()
    monkeypatch.setattr(client, 'publish_pickup_if_changed', lambda hub: None)
    monkeypatch.setattr(kanban_source_clones, 'attest_if_due', lambda hub: None)
    # Prime a real reachable-age sample on the broken board. A strict exception
    # must invalidate it even though the other board successfully completes.
    with kbc.write_txn(live):
        episodes.begin(live, binding()['board_incarnation'], 'fixture-task')
        old_epoch = episodes.epoch(live)
    before = custody(live)
    original_ids = {row[0] for row in live.execute('SELECT op_id FROM kanban_sync_outbox')}
    results = client.sync_all_once(hub, boards=[('default', broken_path), ('healthy', healthy_path)])
    assert [item.board for item in results] == ['default', 'healthy']
    assert results[0].error is not None
    assert ('OSError' if defect == 'transport' else 'ReplayFault') in results[0].error
    assert results[1].error is None, results[1].error
    assert results[1].mode == 'shadow'
    assert custody(live) == before
    assert episodes.epoch(live)[1] != old_epoch[1], 'broken board durable sample token was not invalidated'
    assert broken.accepted == []
    with kbc.connect(healthy_path) as healthy:
        assert journal.snapshot(healthy)['settled'] == str(expected)
        assert journal.pending_wire(healthy) is None
        assert not healthy.execute('SELECT 1 FROM kanban_sync_outbox').fetchone()
        assert healthy.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] > 0
    assert good.h == good.a == expected
    assert len(good.accepted) == expected
    assert {'capability', 'inspect', 'session', 'ops', 'ack'} <= set(good.calls)
    # Restoring the failed prerequisite allows the same untouched first intent
    # to progress, without undoing or reapplying the healthy stream's settlement.
    hub.repaired = True
    result = client.push(live, hub, 'default', 'macbook', primary=False, max_batches=1)
    assert result.applied == 1
    assert broken.accepted[0]['wire']['client_op_id'] in original_ids
    assert good.h == good.a == expected
