"""Lost/NULL/missing replay anchors fail closed at every journal consumer."""
import pytest

from hermes_cli import kanban_actionability_transport as act
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_replay_admission as admission
from hermes_cli import kanban_retain_transport as retain
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_controls as controls
from tests.hermes_cli.test_kanban_replay_lost_anchor_cleanup import consume, custody
from tests.hermes_cli.test_kanban_replay_parent_custody import issued_parent
from tests.hermes_cli import test_kanban_replay_sender as sender

live = sender.live

# Inventory: send, import, strip, prune, retire, echo, retain confirm, actionability confirm.
PLACES = (
    'send', 'import', 'strip', 'prune', 'retire', 'echo',
    'confirm_retain', 'confirm_actionability',
)


def damage_anchor(conn, damage):
    with kbc.write_txn(conn):
        if damage == 'null':
            conn.execute('UPDATE kanban_sync_state SET journal_id=NULL')
        else:
            conn.execute('DELETE FROM kanban_sync_state')


def invoke(conn, place, tid, op_id, owned, peer):
    if place == 'send':
        client.push(conn, peer, 'default', 'macbook', primary=True, max_batches=1)
    elif place == 'confirm_retain':
        retain.confirm(conn, peer, 'default', owned['run_uid'])
    elif place == 'confirm_actionability':
        act.confirm(conn, peer, 'default', owned['op_id'])
    else:
        consume(conn, tid, op_id, place)


@pytest.mark.parametrize('damage', ['null', 'missing'])
@pytest.mark.parametrize('place', PLACES)
def test_lost_anchor_fails_closed_at_every_consumer(live, damage, place):
    peer = controls.Controls()
    peer.lose_reply = True
    if place.startswith('confirm_'):
        kind = 'retain' if place == 'confirm_retain' else act.KIND
        tid, owned, _ = controls.reserve(live, kind)
        with pytest.raises(OSError):
            client.push(live, peer, 'default', 'macbook', primary=True, max_batches=1)
        op_id = owned['op_id']
    else:
        tid, peer, _ = issued_parent(live)
        owned = None
        op_id = live.execute('SELECT op_id FROM kanban_sync_outbox LIMIT 1').fetchone()[0]
    before = custody(live)
    damage_anchor(live, damage)
    calls = []
    original = peer.ops

    def ops(board, wires):
        calls.extend(wires)
        return original(board, wires)

    peer.ops = ops
    refusal = (ValueError, RuntimeError, client.SyncUnavailable, admission.RestoreRequired)
    with pytest.raises(refusal):
        invoke(live, place, tid, op_id, owned, peer)
    assert calls == []
    after = custody(live)
    assert after['kanban_replay_sender1'] == before['kanban_replay_sender1']
    assert after['kanban_sync_outbox'] == before['kanban_sync_outbox']


@pytest.mark.parametrize('kind', ['retain', act.KIND])
def test_never_enrolled_confirmation_may_use_legacy(live, kind):
    _, owned, _ = controls.reserve(live, kind)
    peer = controls.Controls()
    with kbc.write_txn(live):
        live.execute('DROP TABLE kanban_replay_sender1')
        live.execute('UPDATE kanban_sync_state SET journal_id=NULL')
    calls = []

    def ops(board, wires):
        calls.extend(wires)
        return {'results': [{'client_op_id': wires[0]['client_op_id'], 'status': 'applied'}]}

    peer.ops = ops
    try:
        if kind == 'retain':
            retain.confirm(live, peer, 'default', owned['run_uid'])
        else:
            act.confirm(live, peer, 'default', owned['op_id'])
    except (ValueError, AttributeError, IndexError, KeyError):
        pass
    assert calls, 'genuinely never-enrolled boards may still take legacy transport'
