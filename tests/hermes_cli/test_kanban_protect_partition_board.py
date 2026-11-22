"""Real HTTP regression: confirmation preserves the home live-file partition."""

import time
import uuid

from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_protect_stopped_board as support

composed = support.composed


def _foreign_card(c):
    board, ah = c['board'], c['actor_hub']
    tid = 't_' + uuid.uuid4().hex[:12]
    op = {'client_op_id': 'up-' + uuid.uuid4().hex, 'op': 'upsert', 'table': 'tasks', 'uid': tid,
          'fields': {'title': 'another gateway work', 'status': 'ready', 'assignee': 'default', 'created_at': 1000},
          'hlc': f'{time.time_ns() // 1_000_000:013d}-0000-fixture'}
    answer = ah.ops(board, [op])
    assert answer['results'][0]['status'] in ('applied', 'merged'), answer
    return tid


def test_confirm_keeps_live_file_partitioned(composed, request, monkeypatch):
    from hermes_cli import kanban_protect_transport as transport
    c = composed
    conn, hub, board = c['conn'], c['hub'], c['board']
    home = hub.ident.gateway
    foreign = 'foreign' in request.node.name
    other = _foreign_card(c)

    # The production live pull is partitioned (kanban_sync_backlog: pull(..., partition=gateway)).
    client.pull(conn, hub, board, observers=True, partition=home)
    before = {r[0]: r[1] for r in conn.execute('SELECT id, created_on FROM tasks')}

    other2 = _foreign_card(c)  # a foreign change after the live cursor
    assert hub.ops(board, [c['wire']])['results'][0]['status'] == 'applied'
    actual_pull = client.pull
    calls = []
    def observed_pull(*args, **kwargs):
        calls.append(kwargs)
        return actual_pull(*args, **kwargs)
    monkeypatch.setattr(client, 'pull', observed_pull)
    transport.confirm(conn, hub, board, c['request_uid'])
    assert calls == [{'observers': True, 'partition': home}]
    after = {r[0]: r[1] for r in conn.execute('SELECT id, created_on FROM tasks')}

    leaked = sorted(t for t, gw in after.items() if gw not in (None, home))
    assert (other2 in after) is not foreign
    if foreign:
        assert other not in before
        assert not leaked, f'confirm() imported another gateway\'s cards into the home live file: {leaked}'
