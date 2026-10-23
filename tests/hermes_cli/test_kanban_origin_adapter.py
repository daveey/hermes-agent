"""Exercise the registered courier entry with actual session/replica/ledger DBs."""
import argparse
import io
import json
import sqlite3

import pytest

from hermes_cli import kanban, profiles
from hermes_cli.kanban_parser import build_parser
from hermes_cli import kanban_origin_ledger as ledger
from hermes_state import SessionDB


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / 'hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(profiles, '_get_default_hermes_home', lambda: home)
    monkeypatch.setenv('HERMES_PROFILE_NAME', 'wrong-inherited-pin')
    hub = tmp_path / 'hub'
    hub.mkdir()
    monkeypatch.setenv('HERMES_HUB_STATE', str(hub))
    cfg = tmp_path / 'mail.json'
    cfg.write_text(json.dumps({'gateway': 'origin'}))
    monkeypatch.setenv('HERMES_MAIL_CONFIG', str(cfg))
    binding = {
        'binding_id': 'binding-0001', 'version': 1,
        'owner': {'gateway': 'owner', 'profile': 'work', 'board': 'build', 'task_id': 't_one'},
        'origin': {'gateway': 'origin', 'profile': 'default', 'session': 's_old'},
        'task_record_key': 'work/build/t_one', 'publication_version': 3,
    }
    with sqlite3.connect(hub / 'hub.db') as db:
        db.execute('CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)')
        db.execute("INSERT INTO meta VALUES ('gateway', 'origin')")
        db.execute('CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, data TEXT)')
        task = {**binding['owner'], 'id': 't_one', 'status': 'running',
                'origin_bindings': [{k: v for k, v in binding.items() if k != 'publication_version'}]}
        db.execute("INSERT INTO replica VALUES ('owner','tasks','work/build/t_one',3,?)", (json.dumps(task),))
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('s_old', 'cli')
    db.end_session('s_old', 'compression')
    db.create_session('s_new', 'cli', parent_session_id='s_old')
    db.close()
    request = {'id': 'request-0001', 'from': 'gw:owner', 'to_gw': 'origin',
               'coll': 'sessions', 'key': 'default/s_old', 'action': 'origin-bind',
               'args': {'binding': binding}}
    return home, hub, cfg, request


def invoke(world, monkeypatch, capsys):
    wrap = argparse.ArgumentParser()
    parser = build_parser(wrap.add_subparsers())
    args = parser.parse_args(['origin-bind', '--request-file', '-', '--json'])
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(world[3])))
    # This entry has its own ledger; an unrelated broken current board must not
    # interfere, nor may receipt acceptance initialize an empty board.
    monkeypatch.setattr(kanban.kb, 'init_db', lambda: pytest.fail('unexpected board initialization'))
    assert kanban.kanban_command(args) == 0
    return json.loads(capsys.readouterr().out)


def test_registered_adapter_accepts_and_replays_durable_receipt(world, monkeypatch, capsys):
    result = invoke(world, monkeypatch, capsys)
    assert result['status'] == 'accepted'
    assert result['resolved_session'] == 's_new'
    assert result['origin']['profile'] == 'default'
    assert ledger.receipt(world[0] / 'kanban' / 'origin-ledger.db', 'request-0001') == result
    with sqlite3.connect(world[1] / 'hub.db') as db:
        db.execute('DELETE FROM replica')
    assert invoke(world, monkeypatch, capsys) == result


def test_delegated_child_cannot_accept_obligations(world, monkeypatch, capsys):
    monkeypatch.setattr('agent.delegation_context.kanban_path_is_fenced', lambda path: True)
    args = argparse.Namespace(kanban_action='origin-bind', request_file='-')
    assert kanban.kanban_command(args) != 0
    assert not (world[0] / 'kanban/origin-ledger.db').exists()


def test_delegated_fence_checks_actual_ledger_not_board_override(world, monkeypatch, capsys):
    home = world[0]
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home.parent / 'scratch-board'))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.setattr('agent.delegation_context.kanban_path_is_fenced',
                        lambda path: path == home / 'kanban/origin-ledger.db')
    args = argparse.Namespace(kanban_action='origin-bind', request_file='-')
    assert kanban.kanban_command(args) != 0
    assert not (home / 'kanban/origin-ledger.db').exists()


def test_malformed_nested_identity_returns_structured_rejection(world, monkeypatch, capsys):
    world[3]['args']['binding']['owner'] = ['gateway', 'profile', 'board', 'task_id']
    result = invoke(world, monkeypatch, capsys)
    assert result == {'status': 'rejected', 'code': 'origin_binding_invalid', 'handoff_complete': False}


@pytest.mark.parametrize('field,value', [
    ('to_gw', 'other'), ('from', 'gw:impostor'), ('coll', 'tasks'),
    ('action', 'archive'), ('key', 'work/s_old'), ('key', 'default/s_new'),
])
def test_envelope_cannot_retarget_local_identity(world, monkeypatch, capsys, field, value):
    world[3][field] = value
    result = invoke(world, monkeypatch, capsys)
    assert result['status'] == 'rejected'
    assert result['code'] == 'origin_route_mismatch'
    assert not (world[0] / 'kanban/origin-ledger.db').exists()


@pytest.mark.parametrize('damage', ['conflict', 'missing', 'corrupt'])
def test_gateway_identity_is_fail_closed(world, monkeypatch, capsys, damage):
    if damage == 'conflict':
        world[2].write_text(json.dumps({'gateway': 'elsewhere'}))
    elif damage == 'missing':
        world[2].unlink()
    else:
        (world[1] / 'hub.db').write_bytes(b'not sqlite')
    result = invoke(world, monkeypatch, capsys)
    assert result['status'] in ('pending', 'rejected')
    assert result['handoff_complete'] is False
    assert not (world[0] / 'kanban/origin-ledger.db').exists()


@pytest.mark.parametrize('damage', ['missing_session', 'missing_db', 'cycle', 'cap', 'corrupt'])
def test_bad_lineage_never_acknowledges(world, monkeypatch, capsys, damage):
    path = world[0] / 'state.db'
    if damage == 'missing_db':
        path.unlink()
    elif damage == 'corrupt':
        path.write_bytes(b'not sqlite')
    elif damage == 'cap':
        monkeypatch.setattr('hermes_state_compression._CHAIN_CAP', 1)
    else:
        with sqlite3.connect(path) as db:
            if damage == 'cycle':
                db.execute("UPDATE sessions SET parent_session_id='s_new' WHERE id='s_old'")
                db.execute("UPDATE sessions SET end_reason='compression', ended_at=1 WHERE id='s_new'")
            else:
                db.execute("DELETE FROM sessions WHERE id='s_old'")
    result = invoke(world, monkeypatch, capsys)
    assert result['status'] in ('pending', 'rejected')
    assert result['handoff_complete'] is False
    assert ledger.receipt(world[0] / 'kanban/origin-ledger.db', 'request-0001') is None
    if damage == 'missing_db':
        assert not path.exists()


@pytest.mark.parametrize('kind', ['branch', 'delegate', 'tool', 'reset'])
def test_canonical_lineage_does_not_bind_to_unrelated_child(world, monkeypatch, capsys, kind):
    db = SessionDB(db_path=world[0] / 'state.db')
    config = {'_branched_from': 's_old'} if kind == 'branch' else ({'_delegate_from': 's_old'} if kind == 'delegate' else {})
    db.create_session('s_unrelated', 'tool' if kind == 'tool' else 'cli', parent_session_id='s_old', model_config=config)
    if kind == 'reset':
        db.end_session('s_unrelated', 'reset')
    db.close()
    assert invoke(world, monkeypatch, capsys)['resolved_session'] == 's_new'
