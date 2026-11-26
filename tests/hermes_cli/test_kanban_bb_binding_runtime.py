"""Inert authenticated canonical binding producer; synthetic requests only."""
import copy
import json
import sqlite3

import pytest

from hermes_cli import kanban_bb_binding_runtime as runtime
from hermes_cli.kanban_card_answer import AnswerRefused
from tests.hermes_cli import test_kanban_bb_owner_binding as helpers

binding_db = helpers.db
REQUEST = 'binding-request'


@pytest.fixture
def fixture(binding_db, tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    path = home / 'kanban.db'
    with sqlite3.connect(path) as target:
        binding_db.backup(target)
    monkeypatch.setattr('hermes_constants.get_default_hermes_root', lambda: home)
    monkeypatch.setattr(runtime, '_profile', lambda: ('default', home))
    record = dict(id=REQUEST, from_gw='producer', to_gw='owner', coll='tasks',
                  action='bb-owner-binding', key='default/default/' + helpers.TASK, args={})
    reads = []
    class Records:
        gateway = 'owner'
        def read(self, rid):
            reads.append(rid)
            return copy.deepcopy(record)
    monkeypatch.setattr(runtime, 'Records', Records)
    return home, path, record, reads


def invoke(capsys, args=None):
    code = runtime.main([REQUEST] if args is None else args)
    return code, json.loads(capsys.readouterr().out)


def test_entrypoint_exact_allowlist_and_no_writes(fixture, capsys):
    _, path, _, reads = fixture
    before = path.read_bytes()
    code, out = invoke(capsys)
    assert code == 0 and set(out) == {'ok', 'result'} and out['ok'] is True
    result = out['result']
    assert set(result) == {'version', 'owner_gateway', 'owner_profile', 'board', 'task',
                           'tenant', 'generation', 'choices'}
    assert result['version'] == 1 and result['owner_gateway'] == 'owner'
    assert result['owner_profile'] == 'default' and result['board'] == 'default'
    assert result['task'] == helpers.TASK and result['tenant'] == 'babybear'
    assert result['choices'] == helpers.ASK['choices']
    assert len(result['generation']) == 64 and reads == [REQUEST]
    assert helpers.ASK['action'] not in json.dumps(out)
    assert path.read_bytes() == before


@pytest.mark.parametrize('field,value', [('id','foreign'), ('to_gw','foreign'), ('coll','sessions'),
                                        ('action','card-answer'), ('args',None), ('args',[]),
                                        ('args',{'board':'other'}), ('args',{'answer':{}}),
                                        ('key',None), ('key',False), ('key',[]),
                                        ('key','default/default/t_synthetic/extra'),
                                        ('key','default/default'), ('key','work/default/t_synthetic'),
                                        ('key','default/../t_synthetic'), ('key','default//t_synthetic')])
def test_authenticated_record_shape_refused(fixture, capsys, field, value):
    _, path, record, _ = fixture
    before = path.read_bytes()
    record[field] = value
    code, out = invoke(capsys)
    expected = ('owner_board_invalid' if field == 'key' and value in
                ('default/../t_synthetic', 'default//t_synthetic') else 'bb_binding_request_invalid')
    assert code == 2 and out == {'ok': False, 'refused': expected}
    assert path.read_bytes() == before


@pytest.mark.parametrize('field', ['id','to_gw','coll','action','key','args'])
def test_missing_record_fields_refuse(fixture, capsys, field):
    fixture[2].pop(field)
    code, out = invoke(capsys)
    assert code == 2 and out['refused'] == 'bb_binding_request_invalid'


@pytest.mark.parametrize('args', [[], [REQUEST,'forged-envelope'], ['--board','foreign'],
                                  [REQUEST, json.dumps({'args':{},'to_gw':'owner'})]])
def test_cli_has_no_envelope_or_route_override(fixture, capsys, args):
    code, out = invoke(capsys, args)
    assert code == 64 and out == {'ok':False,'error':'usage'}
    assert fixture[3] == []


@pytest.mark.parametrize('record', [None, [], 'envelope', True])
def test_non_object_readback_refuses(fixture, monkeypatch, capsys, record):
    monkeypatch.setattr(runtime.Records, 'read', lambda self, rid: record)
    code, out = invoke(capsys)
    assert code == 2 and out['refused'] == 'bb_binding_request_invalid'


def test_nondefault_board_is_runtime_resolved_not_worker_pin(fixture, monkeypatch, capsys):
    home, path, record, _ = fixture
    board = home / 'kanban/boards/synthetic'
    board.mkdir(parents=True)
    path.rename(board / 'kanban.db')
    with sqlite3.connect(board / 'kanban.db') as conn:
        conn.execute("UPDATE kanban_sync_state SET board_id='synthetic'")
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'injected.db'))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home / 'other'))
    record['key'] = 'default/synthetic/' + helpers.TASK
    code, out = invoke(capsys)
    assert code == 0 and out['result']['board'] == 'synthetic'
    assert not (home / 'injected.db').exists()


@pytest.mark.parametrize('kind', ['missing','escape'])
def test_canonical_path_refusal_without_creation(fixture, tmp_path, capsys, kind):
    _, path, _, _ = fixture
    path.unlink()
    if kind == 'escape':
        outside = tmp_path / 'outside.db'
        with sqlite3.connect(outside) as conn:
            conn.execute('CREATE TABLE sentinel(x)')
        path.symlink_to(outside)
    code, out = invoke(capsys)
    assert code == 2 and out['refused'] == ('owner_board_missing' if kind == 'missing' else 'owner_board_invalid')
    assert not path.exists() if kind == 'missing' else path.is_symlink()


@pytest.mark.parametrize('column,value,error', [('tenant','foreign','bb_binding_target_unverified'),
                                             ('created_on','foreign','bb_binding_target_unverified'),
                                             ('status','ready','bb_binding_ask_ineligible'),
                                             ('block_kind','time','bb_binding_ask_ineligible')])
def test_actual_binding_rejects_ineligible_targets(fixture, capsys, column, value, error):
    path = fixture[1]
    with sqlite3.connect(path) as conn:
        conn.execute(f'UPDATE tasks SET {column}=?', (value,))
    before = path.read_bytes()
    code, out = invoke(capsys)
    assert code == 2 and out['refused'] == error
    assert path.read_bytes() == before


def test_actual_binding_refuses_shadow(fixture, capsys):
    with sqlite3.connect(fixture[1]) as conn:
        conn.execute("UPDATE kanban_sync_state SET mode='shadow'")
    code, out = invoke(capsys)
    assert code == 2 and out['refused'] == 'bb_binding_owner_unverified'


def test_missing_schema_is_not_migrated(fixture, capsys):
    with sqlite3.connect(fixture[1]) as conn:
        conn.execute('DROP TABLE task_events')
    before = fixture[1].read_bytes()
    code, out = invoke(capsys)
    assert code == 2 and out['refused'] == 'bb_binding_schema_unavailable'
    assert fixture[1].read_bytes() == before


def test_changed_and_reappeared_identical_ask_new_generation(fixture, capsys):
    _, first = invoke(capsys)
    with sqlite3.connect(fixture[1]) as conn:
        changed = dict(helpers.ASK, evidence='Another synthetic evidence')
        conn.execute('UPDATE tasks SET human_request=?', (json.dumps(changed),))
    _, second = invoke(capsys)
    with sqlite3.connect(fixture[1]) as conn:
        conn.execute('UPDATE tasks SET human_request=?', (json.dumps(helpers.ASK),))
        conn.execute("INSERT INTO task_events(task_id,kind,uid) VALUES (?,'blocked',?)",
                     (helpers.TASK,'00000000-0000-4000-8000-000000000001'))
    _, third = invoke(capsys)
    assert len({v['result']['generation'] for v in (first,second,third)}) == 3


def test_db_connection_is_readonly_even_if_binding_attempts_write(fixture, monkeypatch, capsys):
    def unsafe(conn, *args, **kwargs):
        conn.execute('CREATE TABLE forbidden(x)')
        pytest.fail('read-only connection allowed schema mutation')
    monkeypatch.setattr(runtime, 'binding', unsafe)
    before = fixture[1].read_bytes()
    code, out = invoke(capsys)
    assert code == 3 and out == {'ok':False,'retryable':True,'error':'unavailable'}
    assert fixture[1].read_bytes() == before


@pytest.mark.parametrize('error', [OSError('synthetic'),sqlite3.OperationalError('synthetic')])
def test_unknown_readback_never_returns_binding(fixture, monkeypatch, capsys, error):
    def unavailable(self, rid):
        raise error
    monkeypatch.setattr(runtime.Records, 'read', unavailable)
    code, out = invoke(capsys)
    assert code == 3 and out == {'ok':False,'retryable':True,'error':'unavailable'}


def test_unexpected_failure_not_success(fixture, monkeypatch, capsys):
    def broken(self, rid):
        raise RuntimeError('private synthetic detail')
    monkeypatch.setattr(runtime.Records, 'read', broken)
    code, out = invoke(capsys)
    assert code == 1 and out == {'ok':False,'error':'internal_error','type':'RuntimeError'}


def test_binding_connection_closed_on_success_and_refusal(fixture, monkeypatch, capsys):
    real = runtime.binding
    observed = []
    def probe(conn, *args, **kwargs):
        observed.append(conn)
        return real(conn, *args, **kwargs)
    monkeypatch.setattr(runtime, 'binding', probe)
    assert invoke(capsys)[0] == 0
    with sqlite3.connect(fixture[1]) as conn:
        conn.execute("UPDATE tasks SET tenant='foreign'")
    assert invoke(capsys)[0] == 2
    assert len(observed) == 2
    for conn in observed:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            conn.execute('SELECT 1')
