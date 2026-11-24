"""Owner courier entry must use the registered tool and retain receipt gating."""
import argparse
import io
import json

import pytest

from hermes_cli import kanban, profiles, kanban_db as kb
from hermes_cli.kanban_parser import build_parser
from hermes_cli import kanban_db_connect as kbc
from tools import kanban_tools

_PROFILE_CHECK = kanban_tools._profile_has_kanban_toolset


@pytest.fixture
def world(tmp_path, monkeypatch):
    for key in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_TASK', 'HERMES_ORIGIN_SESSION'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setattr(profiles, '_get_default_hermes_home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HUB_STATE', str(tmp_path / 'hub'))
    cfg = tmp_path / 'mail.json'
    cfg.write_text(json.dumps({'gateway': 'owner'}), encoding='utf-8')
    monkeypatch.setenv('HERMES_MAIL_CONFIG', str(cfg))
    monkeypatch.setattr(kanban_tools, '_profile_has_kanban_toolset', lambda: True)
    # Courier creation requires genuine renewal authority, which exists only on a synced board.
    (tmp_path / 'config.yaml').write_text('kanban:\n  sync:\n    enabled: true\n')
    kb.init_db()
    return {'id': 'request-owner-001', 'from': 'gw:origin', 'to_gw': 'owner',
            'coll': 'tasks', 'action': 'create', 'key': 'default/default',
            'args': {'origin': {'gateway': 'origin', 'profile': 'work', 'session': 'original'},
                     'create': {'title': 'Build', 'assignee': 'default', 'session_id': 'creator'}}}


def invoke(request, monkeypatch, capsys, command=None):
    parser = build_parser(argparse.ArgumentParser().add_subparsers())
    action = command or ('origin-adopt' if request.get('action') == 'origin-adopt' else 'origin-create')
    args = parser.parse_args([action, '--request-file', '-', '--json'])
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(request)))
    assert kanban.kanban_command(args) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize('config', ['', 'platform_toolsets:\n  cli: [kanban]\n'])
def test_real_cli_process_replays_persisted_creation(world, tmp_path, config):
    import os
    from pathlib import Path
    import subprocess
    import sys

    (tmp_path / 'config.yaml').write_text('kanban:\n  sync:\n    enabled: true\n' + config, encoding='utf-8')
    command = [sys.executable, '-m', 'hermes_cli.main', '-p', 'default',
               'kanban', 'origin-create', '--request-file', '-', '--json']
    results = []
    for _ in range(2):
        run = subprocess.run(command, input=json.dumps(world), text=True, encoding='utf-8', capture_output=True,
                             cwd=Path(__file__).parents[2], env=dict(os.environ), timeout=60, check=False)
        assert run.returncode == 0, run.stderr
        result = json.loads(run.stdout)
        assert result.get('created') is True, result
        assert result['handoff_complete'] is False
        results.append(result)
    assert results[0]['origin_creation'] == results[1]['origin_creation']
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1


@pytest.mark.parametrize('damage', ['pinned_db', 'foreign_home', 'corrupt_board'])
def test_effective_route_and_storage_fail_closed(world, tmp_path, monkeypatch, capsys, damage):
    if damage == 'pinned_db':
        monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'wrong.db'))
        monkeypatch.setattr(kb, '_explicit_board_intent_pinned', lambda: True)
    elif damage == 'foreign_home':
        monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path / 'foreign'))
    else:
        (tmp_path / 'kanban.db').write_bytes(b'not sqlite')
    result = invoke(world, monkeypatch, capsys)
    assert result.get('handoff_complete') is False
    assert result['code'] == ('origin_dispatch_pending' if damage == 'corrupt_board' else 'origin_route_mismatch')
    assert not (tmp_path / 'wrong.db').exists()
    assert not (tmp_path / 'foreign/kanban.db').exists()


@pytest.mark.parametrize('change', ['missing_creator', 'board_override', 'malformed_create', 'extra_args'])
def test_invalid_creation_arguments_create_nothing(world, monkeypatch, capsys, change):
    if change == 'missing_creator':
        world['args']['create'].pop('session_id')
    elif change == 'board_override':
        world['args']['create']['board'] = 'other'
    elif change == 'malformed_create':
        world['args']['create'] = []
    else:
        world['args']['ignored'] = True
    result = invoke(world, monkeypatch, capsys)
    assert result['status'] == 'rejected'
    assert result['handoff_complete'] is False
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0


@pytest.mark.parametrize('field,value', [
    ('to_gw', 'elsewhere'), ('from', 'gw:impostor'), ('coll', 'sessions'),
    ('action', 'archive'), ('key', 'other/default'), ('key', 'default/default/extra'),
    ('key', 'default/../bad'), ('key', 'default/missing'),
])
def test_wrong_route_creates_nothing(world, monkeypatch, capsys, field, value):
    world[field] = value
    result = invoke(world, monkeypatch, capsys)
    assert result['status'] == 'rejected'
    assert result['handoff_complete'] is False
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0


def test_delegated_cli_fence_applies(world, monkeypatch, capsys):
    monkeypatch.setattr('agent.delegation_context.kanban_path_is_fenced', lambda path: True)
    args = argparse.Namespace(kanban_action='origin-create', request_file='-')
    assert kanban.kanban_command(args) != 0


@pytest.mark.parametrize('available', [False, 'absent'])
def test_missing_tool_is_typed_preflight_without_cards(world, monkeypatch, capsys, available):
    from tools.registry import registry
    if available == 'absent':
        monkeypatch.setattr(registry, '_snapshot_entries', list)
    else:
        monkeypatch.setattr(kanban_tools, '_profile_has_kanban_toolset', lambda: False)
    result = invoke(world, monkeypatch, capsys)
    assert result == {'status': 'pending', 'code': 'kanban_tool_unavailable', 'handoff_complete': False}
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0


@pytest.mark.parametrize('adopting', [False, True])
def test_courier_selection_is_scoped_and_preserves_child_fence(world, monkeypatch, capsys, adopting):
    from tools.kanban_toolset_context import kanban_toolset_requested
    monkeypatch.setattr(kanban_tools, '_profile_has_kanban_toolset', _PROFILE_CHECK)
    monkeypatch.setattr(kanban_tools, 'load_config', dict)
    assert not kanban_tools._check_kanban_mode()
    if adopting:
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title='Existing', session_id='creator')
        world = {**world, 'action': 'origin-adopt', 'key': 'default/default/' + tid,
                 'args': {'origin': world['args']['origin']}}
    with monkeypatch.context() as child:
        child.setattr(kanban_tools, '_is_delegated_child_context', lambda: True)
        assert invoke(world, monkeypatch, capsys)['code'] == 'kanban_tool_unavailable'
    result = invoke(world, monkeypatch, capsys)
    assert result.get('adopted' if adopting else 'created') is True, result
    assert result['handoff_complete'] is False
    assert kanban_toolset_requested() is None
    assert not kanban_tools._check_kanban_mode()


def test_owner_entry_creates_once_and_never_claims_handoff(world, monkeypatch, capsys):
    result = invoke(world, monkeypatch, capsys)
    assert result['created'] is True
    assert result['ok'] is False
    assert result['handoff_complete'] is False
    assert result['code'] == 'origin_bind_pending'
    assert invoke(world, monkeypatch, capsys)['origin_creation'] == result['origin_creation']
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
        assert kb.get_task(conn, result['task_id']).session_id == 'creator'
        assert result['origin_creation']['binding']['origin'] == world['args']['origin']


@pytest.mark.parametrize('command', ['origin-create', 'origin-adopt'])
def test_owner_cli_operation_mismatch_is_rejected(world, monkeypatch, capsys, command):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Existing', session_id='creator')
    request = world if command == 'origin-adopt' else {
        **world, 'action': 'origin-adopt', 'key': 'default/default/' + tid,
        'args': {'origin': world['args']['origin']}}
    result = invoke(request, monkeypatch, capsys, command=command)
    assert result.get('status') == 'rejected', result
    assert result['code'] == 'origin_route_mismatch'
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='origin_creation_intents'").fetchone()


@pytest.mark.parametrize('conflict', ['same_id_changed_origin', 'same_origin_new_id'])
def test_adoption_immutable_conflict_is_terminal(world, monkeypatch, capsys, conflict):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Existing', session_id='creator')
    request = {**world, 'action': 'origin-adopt', 'key': 'default/default/' + tid,
               'args': {'origin': world['args']['origin']}}
    assert invoke(request, monkeypatch, capsys)['adopted'] is True
    if conflict == 'same_id_changed_origin':
        request['args'] = {'origin': {**request['args']['origin'], 'session': 'different'}}
        code = 'origin_creation_conflict'
    else:
        request['id'] = 'different-request-001'
        code = 'origin_already_bound'
    result = invoke(request, monkeypatch, capsys)
    assert result['code'] == code
    assert result.get('status') == 'rejected'
    assert result['handoff_complete'] is False


def test_owner_entry_adopts_existing_task_and_replays(world, monkeypatch, capsys):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Existing', session_id='original-creator')
        before = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    request = {**world, 'action': 'origin-adopt', 'key': 'default/default/' + tid,
               'args': {'origin': world['args']['origin']}}
    result = invoke(request, monkeypatch, capsys)
    assert result.get('adopted') is True, result
    assert result['created'] is False
    assert result.get('status', 'pending') == 'pending'
    assert result['code'] == 'origin_bind_pending'
    assert result['handoff_complete'] is False
    replay = invoke(request, monkeypatch, capsys)
    assert replay.get('status', 'pending') == 'pending'
    assert replay['code'] == 'origin_bind_pending'
    assert replay['origin_creation'] == result['origin_creation']
    assert replay['origin_creation']['binding']['origin'] == world['args']['origin']
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
        assert dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()) == before


@pytest.mark.parametrize('damage', ['sender', 'gateway', 'profile', 'task', 'args', 'pinned_db', 'tool_absent', 'delegated'])
def test_adoption_entry_rejects_wrong_route_or_missing_capability(world, monkeypatch, capsys, tmp_path, damage):
    from tools.registry import registry
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Existing', session_id='original-creator')
    request = {**world, 'action': 'origin-adopt', 'key': 'default/default/' + tid,
               'args': {'origin': world['args']['origin']}}
    request_changes = {
        'sender': {'from': 'gw:impostor'},
        'gateway': {'to_gw': 'other'},
        'profile': {'key': 'other/default/' + tid},
        'task': {'key': 'default/default/' + tid[:-1]},
        'args': {'args': {**request['args'], 'create': {'title': 'Duplicate'}}},
    }
    request.update(request_changes.get(damage, {}))
    if damage == 'pinned_db':
        monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'wrong.db'))
        monkeypatch.setattr(kb, '_explicit_board_intent_pinned', lambda: True)
    if damage == 'tool_absent':
        monkeypatch.setattr(registry, '_snapshot_entries', list)
    if damage == 'delegated':
        monkeypatch.setattr('agent.delegation_context.kanban_path_is_fenced', lambda path: True)
        args = argparse.Namespace(kanban_action='origin-adopt', request_file='-')
        assert kanban.kanban_command(args) != 0
        return
    result = invoke(request, monkeypatch, capsys)
    assert result.get('handoff_complete') is False, result
    assert result.get('adopted') is not True
    if damage == 'tool_absent':
        assert result['code'] == 'kanban_tool_unavailable'
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='origin_creation_intents'").fetchone()
