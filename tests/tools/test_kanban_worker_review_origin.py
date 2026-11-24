"""Picked-up workers discover their existing creator chat in the Board shadow."""
import json

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_placement_provenance as provenance
from tests.tools import test_kanban_cross_board_creator_chats as helpers
from tools import kanban_tools as tools

home = helpers.home


@pytest.mark.parametrize('worker', ['none', 'unbound', 'bound'])
def test_ship_review_without_ambient_origin(home, monkeypatch, worker):
    """Exercise kanban_create through actual planning/argv; courier admission is the seam."""
    monkeypatch.delenv('HERMES_ORIGIN_SESSION', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setenv('HERMES_SESSION_ID', helpers.WORKER)
    monkeypatch.setenv('HERMES_SESSION_KEY', helpers.WORKER)
    creator = helpers._replica(home)
    with kbc.connect_closing(home / 'kanban/sync-shadow/default.db') as shadow:
        row = shadow.execute('SELECT * FROM tasks WHERE id=?', (creator,)).fetchone()
    with kbc.connect_closing() as conn:
        # Pickup copies the task/run, not foreign chat links, into the live file.
        columns = list(row.keys())
        conn.execute('INSERT INTO tasks (' + ','.join(columns) + ') VALUES ('
                     + ','.join('?' for _ in columns) + ')', tuple(row))
        conn.execute("UPDATE tasks SET created_on='macbook' WHERE id=?", (creator,))
        conn.commit()
    if worker != 'none':
        monkeypatch.setenv('HERMES_KANBAN_TASK', creator)
    if worker == 'bound':
        monkeypatch.setattr(provenance, 'forwarding_authority', lambda *a, **k: {
            'creator': {'gateway': 'macbook', 'profile': 'default', 'board': 'default', 'task_id': creator},
            'run_uid': 'authenticated-run'})
    monkeypatch.setattr(kp, 'config', lambda: {'worker_queue': {'here': 'pool:worker'}})
    monkeypatch.setattr(kp, 'linux_pool_gateway', lambda *a: True)
    monkeypatch.setattr(kp, 'remote_state', lambda rid: None)
    monkeypatch.setattr(kp, 'await_task_id', lambda *a, **k: ('t_review', 'ready'))
    sent = []

    def courier(argv):
        if argv[-1] == '--help':
            return 0, '--option --canonical-home canonical-home-v2', ''
        sent.append(argv)
        # Installed hermes_hub.py's canonical-home admission: a blank origin
        # refuses before enqueue; a tracked origin continues to authority checks.
        if not argv[argv.index('--origin-session') + 1]:
            return 2, '', 'canonical_home_origin_required: complete payload and tracked origin required'
        return 0, 'queued', ''

    kp._help_cache.clear()
    monkeypatch.setattr(kp, 'hub_runner', courier)
    request = {'title': 'Independent exact-head review PR #123 @' + 'a' * 10,
               'body': 'Review exact head ' + 'a' * 40 + '; require independent verdict and required CI.',
               'assignee': 'default', 'skills': ['sdlc-review'],
               'runs_on': ['pool:worker'], 'requires': [], 'project': '',
               'idempotency_key': 'core-wt-review:123:' + 'a' * 40}
    out = json.loads(tools._handle_create(request))
    if worker == 'unbound':
        assert not sent
        assert out.get('created') is False
        assert 'origin_forward_creator_unknown' in out['error']
        return
    assert len(sent) == 1, out
    if worker == 'none':
        assert out.get('created') is False
        assert 'canonical_home_origin_required' in out['error']
        return
    assert out.get('task_id') == 't_review', out
    argv = sent[0]
    assert argv[argv.index('--origin-session') + 1] == helpers.FOREIGN_CHAT
    assert argv[argv.index('--origin-gateway') + 1] == 'macbook'
    assert argv[argv.index('--creator-task') + 1] == creator
    assert argv[argv.index('--creator-run') + 1] == 'authenticated-run'
    options = dict(v.split('=', 1) for i, v in enumerate(argv) if i and argv[i-1] == '--option')
    assert json.loads(options['body']) == request['body']
    assert json.loads(options['idempotency_key']) == request['idempotency_key']
