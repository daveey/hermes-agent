"""A strict stream's refusal must not abort later boards' real sync rounds."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_source_clones
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as sender

live = sender.live


@pytest.mark.parametrize('defect', ['capability', 'session', 'inspect'])
def test_strict_refusal_isolated_in_real_multiboard_round(live, tmp_path, monkeypatch, defect):
    class RefusingPeer(sender.Receiver):
        refuse = True

        def _call(self, method, path, body=None):
            result = super()._call(method, path, body)
            action = path.rsplit('/', 1)[-1]
            if self.refuse and action == 'capability' and defect == 'capability':
                result['processed_prefix'] = False
            if self.refuse and action == defect and defect in ('session', 'inspect'):
                result['slot'] = dict(result['slot'], journal_id='foreign-journal')
            return result

        def changes(self, board, since, limit=500):
            return dict(changes=[], next=since, head=since, more=False)

    from hermes_cli import kanban_db as kb
    kb.create_task(live, title='unresolved strict custody', assignee='worker')
    peer = RefusingPeer()
    peer.ident = SimpleNamespace(gateway='macbook')
    monkeypatch.setattr(client, 'publish_pickup_if_changed', lambda hub: None)
    monkeypatch.setattr(kanban_source_clones, 'attest_if_due', lambda hub: None)
    healthy = tmp_path / 'healthy.db'
    from hermes_cli import kanban_db_sync as sync
    with kbc.connect(healthy) as conn:
        sync.set_mode(conn, 'off')  # Real detach; later sync_board must return without peer calls.
    first = Path(live.execute('PRAGMA database_list').fetchone()[2])
    before = [tuple(r) for r in live.execute('SELECT * FROM kanban_sync_outbox')]
    results = client.sync_all_once(peer, boards=[('default', first), ('healthy', healthy)])
    assert [r.board for r in results] == ['default', 'healthy']
    assert results[0].error is not None and 'ReplayFault' in results[0].error
    assert results[1].error is None and results[1].mode == 'off'
    assert peer.sent == []
    assert [tuple(r) for r in live.execute('SELECT * FROM kanban_sync_outbox')] == before
    peer.refuse = False
    result = client.push(live, peer, 'default', 'macbook', primary=False, max_batches=1)
    assert result.applied == 1 and len(peer.sent) == 1


def test_legacy_programming_error_is_not_reclassified(live, tmp_path, monkeypatch):
    def broken_legacy(*args, **kwargs):
        raise ValueError('legacy control error')

    monkeypatch.setattr(client, '_push_legacy', broken_legacy)
    with kbc.connect(tmp_path / 'legacy.db') as conn:
        client.ensure_identity(conn, 'legacy', 'macbook')
        with pytest.raises(ValueError, match='legacy control error') as error:
            client.push(conn, sender.Receiver(), 'legacy', 'macbook', primary=False)
    assert type(error.value) is ValueError
