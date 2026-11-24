"""Local/forwarded field contract, including explicit-empty caller intent.

Empty lists are values, not permission to recalculate placement. Empty scalar
identity/workspace values retain the local entrypoint's documented defaults.
Host-local workspaces refuse forwarding; they are never silently discarded.
"""
import json

import pytest

from hermes_cli import kanban_creation_home as creation
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_placement as placement
from tests.hermes_cli import test_kanban_worker_node as nodes
from tests.tools import test_kanban_worker_canonical_home as home
from tools import kanban_tools as tools

svc, gws, pool_labels = home.svc, home.gws, home.pool_labels
MISSING = object()
# field, omitted/default, explicit-empty, set; expected effective value follows
# each input. None means omitted on the wire (receiver applies its default).
CASES = [
    ('runs_on', MISSING, None, ['pool:worker']),
    ('runs_on', [], [], []),
    ('runs_on', ['machine:macbook'], ['machine:macbook'], ['machine:macbook']),
    ('runs_on', 'pool:worker', ['pool:worker'], ['pool:worker']),
    ('runs_on', '["pool:worker"]', ['pool:worker'], ['pool:worker']),
    ('runs_on', '', [], []),
    ('runs_on', '[]', [], []),
    ('runs_on', 'pool:worker,capability:tailnet-exec,pool:worker',
     ['capability:tailnet-exec', 'pool:worker'], ['capability:tailnet-exec', 'pool:worker']),
    ('runs_on', ['pool:worker', 'capability:tailnet-exec', 'pool:worker'],
     ['capability:tailnet-exec', 'pool:worker'], ['capability:tailnet-exec', 'pool:worker']),
    ('tenant', MISSING, 'tenant-source', 'tenant-source'),
    ('tenant', '', 'tenant-source', 'tenant-source'),
    ('tenant', 'tenant-explicit', 'tenant-explicit', 'tenant-explicit'),
    ('requires', MISSING, None, None),
    ('requires', [], [], []),
    ('requires', ['terminal'], ['terminal'], ['terminal']),
    ('assignee', MISSING, 'default', 'default'),
    ('assignee', '', 'default', 'default'),
    ('assignee', 'default', 'default', 'default'),
    ('skills', MISSING, None, None),
    ('skills', [], [], []),
    ('skills', ['five-whys'], ['five-whys'], ['five-whys']),
    ('priority', MISSING, 0, None),
    ('priority', 0, 0, 0),
    ('priority', 17, 17, 17),
    ('parents', MISSING, [], []),
    ('parents', [], [], []),
    ('parents', 'PARENT', 'PARENT', 'PARENT'),
    ('workspace_kind', MISSING, None, None),
    ('workspace_kind', '', '', None),
    ('workspace_kind', 'scratch', 'scratch', None),
    ('session_id', MISSING, 'chat-ambient', 'chat-ambient'),
    ('session_id', '', 'chat-ambient', 'chat-ambient'),
    ('session_id', 'chat-explicit', 'chat-explicit', 'chat-explicit'),
    ('creator_task_id', MISSING, None, 'chat-ambient'),
    ('creator_task_id', '', None, 'chat-ambient'),
    ('creator_task_id', 'PARENT', 'PARENT', 'chat-explicit'),
]


@pytest.mark.parametrize('forwarded', [False, True], ids=['local', 'forwarded'])
@pytest.mark.parametrize('field,value,local_expected,remote_expected', CASES,
                         ids=[f'{row[0]}-{i}' for i, row in enumerate(CASES)])
def test_caller_field_contract(gws, monkeypatch, field, value, local_expected, remote_expected, forwarded):
    _, worker = gws
    worker.enter()
    skill = worker.home / 'skills' / 'five-whys' / 'SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: five-whys\ndescription: Fixture skill\n---\nFixture.\n')
    from hermes_state import SessionDB
    state = SessionDB(db_path=worker.home / 'state.db')
    state.create_session('chat-ambient', source='desktop')
    state.create_session('chat-explicit', source='desktop')
    state.close()
    monkeypatch.setenv('HERMES_SESSION_ID', 'chat-ambient')
    monkeypatch.setenv('HERMES_TENANT', 'tenant-source')
    monkeypatch.setattr(placement.reg, 'self_gateway', lambda: nodes.WORKER)
    monkeypatch.setattr(placement, 'config', lambda: {'worker_queue': {'hub': 'pool:worker'}})
    monkeypatch.setattr(placement, '_machine_picks_up', lambda _: True)
    if not forwarded:
        monkeypatch.setattr(creation, 'plan', lambda *a, **kw: None)
    parent = kb.create_task(worker.conn, title='contract predecessor', assignee='default', requires=[])
    if field == 'creator_task_id':
        # Creator identity is admitted runtime context, never an LLM argument.
        from agent import delegation_context
        from tests.tools.test_kanban_cross_board_creator_chats import _link
        monkeypatch.setattr(tools, '_is_dispatcher_owned_worker', lambda: True)
        monkeypatch.setattr(delegation_context, 'is_dispatcher_owned_worker_context', lambda: True)
        if value == 'PARENT':
            _link(worker.conn, parent, nodes.WORKER, 'default', 'chat-explicit')
            monkeypatch.setenv('HERMES_KANBAN_TASK', parent)
            local_expected = parent
        else:
            monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    elif value == 'PARENT':
        value = [parent]
        local_expected = remote_expected = [parent]
    captured, sent = [], []
    real_create = kb.create_task

    def local_create(conn, **kwargs):
        captured.append(kwargs)
        return real_create(conn, **kwargs)

    def run(argv):
        if argv[-1] == '--help':
            return 0, '--option --canonical-home canonical-home-v2 canonical-home-v1', ''
        sent.append(argv)
        return 3, 'durably queued', ''

    monkeypatch.setattr(kb, 'create_task', local_create)
    monkeypatch.setattr(placement, 'hub_runner', run)
    monkeypatch.setattr(placement, 'remote_state', lambda _: None)
    placement._help_cache.clear()
    args = {'title': 'field contract proof', 'assignee': 'default', 'requires': []}
    args.pop(field, None)
    if value is not MISSING and field != 'creator_task_id':
        args[field] = value
    result = json.loads(tools._handle_create(args))
    if field == 'workspace_kind' and value == '':
        assert not result.get('task_id') and not sent
        assert 'workspace_kind' in result.get('error', '') or result.get('code') == 'placement_unavailable'
        return
    if not forwarded:
        assert result.get('task_id'), result
        assert not sent and len(captured) == 1
        assert captured[0][field] == (tuple(local_expected) if field == 'parents' else local_expected)
        row = kb.get_task(worker.conn, result['task_id'])
        if field in ('assignee', 'tenant', 'priority'):
            assert getattr(row, field) == local_expected
        return
    assert result.get('placed', {}).get('gateway') == 'hub', result
    assert not captured and len(sent) == 1
    argv = sent[0]
    options = {key: json.loads(raw) for key, raw in
               (arg.split('=', 1) for i, arg in enumerate(argv) if i and argv[i-1] == '--option')}
    flag = {'assignee': '--assignee', 'session_id': '--origin-session',
            'creator_task_id': '--origin-session'}.get(field)
    actual = argv[argv.index(flag) + 1] if flag else options.get(field)
    assert actual == remote_expected
    if value is not MISSING and isinstance(value, list):
        assert field in options  # [] must not disappear behind an intake default


@pytest.mark.parametrize('kind', ['dir', 'worktree'])
def test_host_workspace_is_refused_not_lost(gws, monkeypatch, kind):
    _, worker = gws
    worker.enter()
    monkeypatch.setattr(placement.reg, 'self_gateway', lambda: nodes.WORKER)
    monkeypatch.setattr(placement, 'config', lambda: {'worker_queue': {'hub': 'pool:worker'}})
    before = worker.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    result = json.loads(tools._handle_create({'title': 'host source', 'assignee': 'default',
                                             'requires': [], 'workspace_kind': kind}))
    assert result.get('code') == 'placement_unavailable', result
    assert worker.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before
