"""Canonical BB target eligibility only; no producer/decision authority. [daveey]"""
import copy
import json
import sqlite3
from uuid import uuid4

import pytest

from hermes_cli import kanban_bb_owner_binding as bb
from hermes_cli import kanban_card_answer as desktop

TASK = 't_synthetic'
ASK = dict(action='Pick a synthetic plan.', why_human='Only the owner chooses.', interaction='reply',
           scope='Synthetic plan', evidence='Synthetic fixture',
           choices=[dict(label=' Plan A ', value=' a '), dict(label='Plan B', value='b')])


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE kanban_sync_state(board_id TEXT, gateway_id TEXT, mode TEXT);
        INSERT INTO kanban_sync_state VALUES ('default','owner','primary');
        CREATE TABLE tasks(id TEXT PRIMARY KEY, tenant TEXT, created_on TEXT, status TEXT,
                           human_request TEXT, block_kind TEXT);
        CREATE TABLE task_events(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, uid TEXT);
    ''')
    conn.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?)',
                 (TASK, 'babybear', 'owner', 'blocked', json.dumps(ASK), 'needs_input'))
    conn.execute('INSERT INTO task_events(task_id,kind,uid) VALUES (?,?,?)', (TASK, 'blocked', str(uuid4())))
    conn.commit()
    yield conn
    conn.close()


def read(db, **kwargs):
    return bb.binding(db, TASK, **dict(owner_gateway='owner', owner_profile='default', board='default', **kwargs))


def replace_ask(db, ask):
    db.execute('UPDATE tasks SET human_request=? WHERE id=?', (json.dumps(ask), TASK))
    db.commit()


def test_exact_binding_and_desktop_generation_agree_without_writes(db):
    before = db.total_changes
    result = read(db)
    assert result == dict(version=1, owner_gateway='owner', owner_profile='default', board='default', task=TASK,
                          tenant='babybear', generation=desktop.snapshot(db, TASK)['generation'], ask=ASK,
                          choices=ASK['choices'])
    assert db.total_changes == before and not db.in_transaction
    assert result['choices'][0] == {'label': ' Plan A ', 'value': ' a '}


@pytest.mark.parametrize('column,value', [('tenant','hermes'), ('tenant',None), ('created_on','foreign'),
                                        ('created_on',None), ('status','ready'), ('status','running'),
                                        ('block_kind','time'), ('block_kind',None)])
def test_ineligible_target(db, column, value):
    db.execute(f'UPDATE tasks SET {column}=? WHERE id=?', (value,TASK))
    db.commit()
    with pytest.raises(bb.AnswerRefused):
        read(db)


def test_missing_target(db):
    db.execute('DELETE FROM tasks')
    db.commit()
    with pytest.raises(bb.AnswerRefused, match='bb_binding_target_unverified'):
        read(db)


@pytest.mark.parametrize('column,value', [('board_id','other'), ('gateway_id','foreign'), ('mode','shadow'),
                                        ('mode',None)])
def test_owner_requires_exact_board_primary(db, column, value):
    db.execute(f'UPDATE kanban_sync_state SET {column}=?', (value,))
    db.commit()
    with pytest.raises(bb.AnswerRefused, match='bb_binding_owner_unverified'):
        read(db)


def test_foreign_primary_does_not_mask_selected_shadow(db):
    db.execute("UPDATE kanban_sync_state SET mode='shadow'")
    db.execute("INSERT INTO kanban_sync_state VALUES ('other','owner','primary')")
    db.commit()
    with pytest.raises(bb.AnswerRefused, match='bb_binding_owner_unverified'):
        read(db)


@pytest.mark.parametrize('count', [0, 2])
def test_owner_row_must_be_unique(db, count):
    db.execute('DELETE FROM kanban_sync_state')
    for _ in range(count):
        db.execute("INSERT INTO kanban_sync_state VALUES ('default','owner','primary')")
    db.commit()
    with pytest.raises(bb.AnswerRefused, match='bb_binding_owner_unverified'):
        read(db)


@pytest.mark.parametrize('table,column', [('kanban_sync_state','board_id'), ('tasks','created_on'),
                                        ('task_events','uid')])
def test_legacy_missing_schema_not_authority(db, table, column):
    db.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
    with pytest.raises(bb.AnswerRefused, match='bb_binding_schema_unavailable'):
        read(db)


@pytest.mark.parametrize('field,value', [('interaction','physical'), ('interaction','external'),
                                       ('interaction','user_terminal'), ('interaction','unknown'),
                                       ('action',''), ('scope',None), ('why_human',3), ('evidence',None),
                                       ('choices',None), ('choices',[]), ('choices',{}),
                                       ('choices',[{'label':'A','value':'a'}]),
                                       ('choices',[{'label':'A','value':'a'}, {'label':'B','value':'a'}]),
                                       ('choices',[{'label':'A','value':'a','extra':True}, {'label':'B','value':'b'}]),
                                       ('choices',[{'label':'','value':'a'}, {'label':'B','value':'b'}]),
                                       ('choices',['a','b'])])
def test_malformed_or_nondecision_ask(db, field, value):
    replace_ask(db, dict(copy.deepcopy(ASK), **{field:value}))
    with pytest.raises(bb.AnswerRefused, match='bb_binding_ask_ineligible'):
        read(db)


@pytest.mark.parametrize('raw', ['null','[]','true','3','"string"','{'])
def test_unstructured_ask(db, raw):
    db.execute('UPDATE tasks SET human_request=?', (raw,))
    db.commit()
    with pytest.raises(bb.AnswerRefused, match='bb_binding_ask_ineligible'):
        read(db)


@pytest.mark.parametrize('interaction,status,kind', [('reply','blocked','needs_input'),
                                                   ('approval','triage','capability')])
def test_declared_decision_classes(db, interaction, status, kind):
    db.execute('UPDATE tasks SET status=?,block_kind=?', (status,kind))
    replace_ask(db, dict(ASK, interaction=interaction))
    assert read(db)['ask']['interaction'] == interaction


@pytest.mark.parametrize('uid', [None, '', 'local:1', 'not-a-uuid', 'AAAAAAAA-AAAA-4AAA-AAAA-AAAAAAAAAAAA'])
def test_unportable_lifecycle_refuses(db, uid):
    db.execute('UPDATE task_events SET uid=?', (uid,))
    db.commit()
    with pytest.raises(bb.AnswerRefused, match='bb_binding_lifecycle_unverified'):
        read(db)


@pytest.mark.parametrize('kind', ['empty','duplicate'])
def test_complete_unique_lifecycle_required(db, kind):
    if kind == 'empty':
        db.execute('DELETE FROM task_events')
    else:
        db.execute('INSERT INTO task_events(task_id,kind,uid) SELECT task_id,kind,uid FROM task_events')
    db.commit()
    with pytest.raises(bb.AnswerRefused, match='bb_binding_lifecycle_unverified'):
        read(db)


def test_changed_and_reappeared_identical_ask_new_generation(db):
    first = read(db)
    replace_ask(db, dict(ASK, evidence='A changed synthetic premise'))
    assert read(db)['generation'] != first['generation']
    replace_ask(db, ASK)
    assert read(db) == first
    db.execute('INSERT INTO task_events(task_id,kind,uid) VALUES (?,?,?)', (TASK,'blocked',str(uuid4())))
    db.commit()
    assert read(db)['generation'] != first['generation']


def test_lifecycle_not_local_order_and_unrelated_events_ignored(db):
    db.execute('INSERT INTO task_events(id,task_id,kind,uid) VALUES (?,?,?,?)', (9,TASK,'unblocked',str(uuid4())))
    db.commit()
    first = read(db)
    db.execute('UPDATE task_events SET id=id+100')
    db.execute('INSERT INTO task_events(task_id,kind,uid) VALUES (?,?,?)', (TASK,'heartbeat',None))
    db.execute('INSERT INTO task_events(task_id,kind,uid) VALUES (?,?,?)', ('t_other','blocked',None))
    db.commit()
    assert read(db) == first


@pytest.mark.parametrize('field,value', [('owner_gateway','../owner'), ('owner_profile','a/b'), ('board','../default'),
                                       ('owner_profile',''), ('owner_gateway',None)])
def test_invalid_trusted_route(db, field, value):
    args = dict(owner_gateway='owner', owner_profile='default', board='default')
    args[field] = value
    with pytest.raises(bb.AnswerRefused, match='bb_binding_route_invalid'):
        bb.binding(db,TASK,**args)


def test_read_does_not_commit_or_rollback_caller_transaction(db):
    db.execute("UPDATE tasks SET human_request=?", (json.dumps(dict(ASK, evidence='uncommitted')),))
    assert read(db)['ask']['evidence'] == 'uncommitted'
    assert db.in_transaction
    db.rollback()
    assert read(db)['ask'] == ASK


def test_refusal_preserves_callers_transaction(db):
    db.execute("UPDATE tasks SET tenant='foreign'")
    with pytest.raises(bb.AnswerRefused):
        read(db)
    assert db.in_transaction
    db.rollback()
    assert read(db)['tenant'] == 'babybear'


def test_actual_sqlite_authorizer_disallows_mutations(db):
    deny = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_DROP_TABLE}
    db.set_authorizer(lambda op,*_: sqlite3.SQLITE_DENY if op in deny else sqlite3.SQLITE_OK)
    assert read(db)['task'] == TASK
    assert not db.in_transaction


def test_snapshot_consistent_when_other_connection_changes_ask(db, tmp_path):
    path = tmp_path/'canonical.db'
    other = sqlite3.connect(path)
    db.backup(other)
    other.execute('PRAGMA journal_mode=WAL')
    other.commit()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    raced = []
    def traced(sql):
        if sql.startswith('SELECT tenant,') and not raced:
            raced.append(True)
            other.execute('UPDATE tasks SET human_request=?', (json.dumps(dict(ASK, evidence='later')),))
            other.commit()
    conn.set_trace_callback(traced)
    try:
        assert read(conn)['ask'] == ASK
        assert raced
        conn.set_trace_callback(None)
        assert read(conn)['ask']['evidence'] == 'later'
    finally:
        conn.close()
        other.close()
