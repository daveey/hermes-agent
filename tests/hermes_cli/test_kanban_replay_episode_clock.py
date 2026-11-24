"""Successful predecessor time never ages a new replay recovery episode."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_replay_recovery as recovery
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers

live = helpers.live


@pytest.mark.parametrize('failure_seconds', [0, 35, 300])
def test_successful_predecessors_do_not_age_successor_failure(live, monkeypatch, failure_seconds):
    now = [1000.0]
    monkeypatch.setattr(recovery, 'time', lambda: now[0])
    for i in range(12):
        kb.create_task(live, title=f'task {i}', assignee='worker')

    class Peer(helpers.Receiver):
        def _call(self, method, path, body=None):
            if path.endswith('/ops'):
                if len(self.sent) == 20:
                    now[0] += failure_seconds
                    raise OSError('first failure of operation 21')
                now[0] += 15.0
            return super()._call(method, path, body)

    peer = Peer()
    with pytest.raises(OSError, match='first failure'):
        client.push(live, peer, 'default', 'macbook', primary=False, max_batches=25)
    held = recovery.fault(live)
    assert len(peer.sent) == 20 and journal.snapshot(live)['settled'] == '20'
    assert held['sequence'] == '21' and held['op_id'] == journal.snapshot(live)['pending']['op_id']
    assert held['attempts'] == 1
    assert held['reachable_s'] == failure_seconds, 'predecessor time charged to successor'
    assert held['paused'] == int(failure_seconds >= 300)
    reopened = helpers.reopen(live)
    try:
        assert recovery.fault(reopened) == held
    finally:
        reopened.close()
