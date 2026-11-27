"""Recovered owner receipts release only an exact qualified historical carry. [daveey]"""
import hashlib
import json
import shutil
import sqlite3
import time

import pytest

from hermes_cli import kanban_terminal_carry as carry


def test_readers_guard_current_databases_but_not_immutable_evidence(tmp_path, monkeypatch):
    from hermes_cli import kanban_lockguard

    path = tmp_path / 'readers.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE evidence(value TEXT)')
    calls = []
    original = kanban_lockguard.connect

    def guarded(database, **kwargs):
        calls.append(database)
        return original(database, **kwargs)

    monkeypatch.setattr(kanban_lockguard, 'connect', guarded)
    for immutable in (False, True):
        conn = carry._ro(path, immutable=immutable)
        try:
            assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
            assert conn.execute('SELECT count(*) FROM evidence').fetchone()[0] == 0
        finally:
            conn.close()
    assert calls == [path.resolve().as_uri() + '?mode=ro']


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def world(tmp_path, monkeypatch):
    import hermes_constants
    from hermes_cli import profiles, kanban_origin_adapter, kanban_sync_client
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(kanban_origin_adapter, '_gateway', lambda path: 'origin')
    owner = tmp_path / 'owner.db'
    replica = tmp_path / 'replica.db'
    state = tmp_path / 'state.db'
    receipt = tmp_path / 'kanban' / 'terminal-carry' / 'receipt.json'
    card = dict(id='t_1', gateway='owner', profile='default', board='default',
                origin_gateway='origin', origin_profile='default', session='human1',
                via='shepherd', key='owner/default/default/t_1', ownership_state='qualified',
                status='unknown', reported_status='done', unfinished=True, released=False, stale=True)
    with sqlite3.connect(owner) as conn:
        conn.executescript('''
          CREATE TABLE tasks(id TEXT,created_on TEXT,status TEXT,completed_at INTEGER,current_run_id INTEGER,
            goal_mode INTEGER,completion_contract TEXT,title TEXT,body TEXT,created_at INTEGER);
          INSERT INTO tasks VALUES('t_1','owner','done',100,NULL,0,'local-only','Title','Body',1);
          CREATE TABLE task_events(id INTEGER,task_id TEXT,run_id INTEGER,kind TEXT,created_at INTEGER,payload TEXT,uid TEXT);
          INSERT INTO task_events VALUES(2,'t_1',7,'completed',100,'{}','completion-uid');
          CREATE TABLE task_runs(id INTEGER,task_id TEXT,status TEXT,outcome TEXT,ended_at INTEGER,summary TEXT,profile TEXT);
          INSERT INTO task_runs VALUES(7,'t_1','completed','completed',100,'Done','default');
          CREATE TABLE kanban_notify_subs(task_id TEXT,platform TEXT,chat_id TEXT,delivery_mode TEXT,delivery_metadata TEXT);
          INSERT INTO kanban_notify_subs VALUES('t_1','hub','origin/default/human1','notify','{"reason":"create"}');
          CREATE TABLE task_chat_links(uid TEXT,task_id TEXT,chat_gateway TEXT,chat_profile TEXT,chat_session TEXT,kind TEXT,released INTEGER);
          INSERT INTO task_chat_links VALUES('t_1:origin:default:human1:shepherd','t_1','origin','default','human1','shepherd',0);
          INSERT INTO task_chat_links VALUES('t_1:origin:-:human1:created','t_1','origin',NULL,'human1','created',1);
          CREATE TABLE kanban_sync_state(pulled_at REAL);
        ''')
        conn.execute('INSERT INTO kanban_sync_state VALUES(?)', (time.time(),))
    shutil.copyfile(owner, replica)
    # Replica events use different integer IDs; portable UIDs carry generation.
    with sqlite3.connect(replica) as conn:
        conn.execute('UPDATE task_events SET id=999')
    with sqlite3.connect(state) as conn:
        conn.executescript("CREATE TABLE sessions(id TEXT, source TEXT); INSERT INTO sessions VALUES('human1','desktop');")
    monkeypatch.setattr(kanban_sync_client, 'shadow_path', lambda board: replica)
    return dict(owner=owner, replica=replica, state=state, receipt=receipt, card=card)


def register(w, **overrides):
    args = dict(owner_db=w['owner'], owner_sha256=digest(w['owner']), card=w['card'],
                replica=w['replica'], state_db=w['state'], gateway='origin', profile='default')
    args.update(overrides)
    return carry.register(w['receipt'], **args)


def edit(path, sql):
    with sqlite3.connect(path) as conn:
        conn.execute(sql)


def test_verified_exact_carry_releases_without_aliasing_null(world):
    w = world
    before = digest(w['owner']), digest(w['replica']), digest(w['state'])
    register(w)
    assert before == (digest(w['owner']), digest(w['replica']), digest(w['state']))
    row = carry.reconcile_carry([w['card']])[0]
    assert row['released'] is True and row['unfinished'] is False and row['status'] == 'done'
    assert row['stale'] is True  # historical receipt, never fresh owner liveness
    assert carry.reconcile_carry([{**w['card'], 'origin_profile': None}])[0]['released'] is False
    assert carry.reconcile_carry([{**w['card'], 'origin_profile': 'work'}])[0]['released'] is False


@pytest.mark.parametrize('field,value', [('origin_gateway', 'elsewhere'), ('origin_profile', 'work'),
                                        ('origin_profile', None), ('profile', 'work'), ('gateway', 'other'),
                                        ('session', 'someone_else'), ('ownership_state', 'unknown')])
def test_registration_rejects_wrong_or_ambiguous_identity(world, field, value):
    with pytest.raises(ValueError):
        register(world, card={**world['card'], field: value})
    assert not world['receipt'].exists()


@pytest.mark.parametrize('sql', [
    "DELETE FROM task_runs", "DELETE FROM task_events", "UPDATE tasks SET current_run_id=7",
    "UPDATE tasks SET goal_mode=1", "UPDATE tasks SET completion_contract='https://github.com/o/r/pull/1'",
    "UPDATE tasks SET status='ready'", "UPDATE task_runs SET outcome='failed'",
    "UPDATE task_events SET run_id=8", "UPDATE task_events SET uid=NULL",
    "UPDATE kanban_notify_subs SET chat_id='origin/work/human1'",
    "UPDATE kanban_notify_subs SET delivery_mode='touch'",
    "INSERT INTO kanban_notify_subs VALUES('t_1','hub','origin/work/human1','notify','{\"reason\":\"create\"}')",
    "DELETE FROM task_chat_links WHERE kind='shepherd'",
    "UPDATE task_chat_links SET chat_profile=NULL WHERE kind='shepherd'",
    "UPDATE task_runs SET profile='work'",
    "UPDATE kanban_notify_subs SET delivery_metadata='[]'",
])
def test_registration_requires_real_owner_completion_and_exact_origin(world, sql):
    edit(world['owner'], sql)
    with pytest.raises(ValueError):
        register(world)
    assert not world['receipt'].exists()


@pytest.mark.parametrize('sql', [
    "UPDATE tasks SET status='ready'", "UPDATE tasks SET completed_at=101", "UPDATE tasks SET body='Edited'",
    "UPDATE tasks SET created_on='other'", "UPDATE task_events SET uid='new-completion'", "DELETE FROM tasks",
])
def test_current_generation_mismatch_refuses_registration_and_release(world, sql):
    register(world)
    released = carry.reconcile_carry([world['card']])[0]
    edit(world['replica'], sql)
    assert carry.reconcile_carry([released])[0]['released'] is False
    with pytest.raises(ValueError):
        register(world)


@pytest.mark.parametrize('failure', ['missing', 'hash', 'proof', 'duplicate', 'stale', 'future', 'missing_directory'])
def test_missing_or_changed_proof_restores_unknown(world, failure):
    w = world
    register(w)
    released = carry.reconcile_carry([w['card']])[0]
    def change_proof():
        receipt = json.loads(w['receipt'].read_text())
        receipt['proof']['completion_uid'] = 'fabricated'
        w['receipt'].write_text(json.dumps(receipt))

    def remove_directory():
        w['receipt'].unlink()
        w['receipt'].parent.rmdir()

    mutations = {
        'missing': w['owner'].unlink,
        'hash': lambda: edit(w['owner'], "UPDATE tasks SET title='Tampered'"),
        'proof': change_proof,
        'duplicate': lambda: shutil.copyfile(w['receipt'], w['receipt'].with_name('second.json')),
        'stale': lambda: edit(w['replica'], 'UPDATE kanban_sync_state SET pulled_at=1'),
        'future': lambda: edit(w['replica'], 'UPDATE kanban_sync_state SET pulled_at=99999999999'),
        'missing_directory': remove_directory,
    }
    mutations[failure]()
    row = carry.reconcile_carry([released])[0]
    assert row['released'] is False and row['unfinished'] is True and row['status'] == 'unknown'


def test_no_receipt_and_fresh_rows_unchanged(world):
    assert carry.reconcile_carry([world['card']]) == [world['card']]
    register(world)
    live = {**world['card'], 'stale': False, 'status': 'ready'}
    assert carry.reconcile_carry([live]) == [live]


def test_snapshot_carry_integration(world, monkeypatch):
    from hermes_cli import kanban_session_activity as activity
    w = world
    register(w)
    monkeypatch.setattr(activity, '_remote_last', None)
    monkeypatch.setattr(activity, '_snapshot_saved', None)
    monkeypatch.setattr(activity, '_remote_snapshot', lambda cards=None: [w['card']] if cards is None else cards)
    monkeypatch.setattr(activity, '_hub_db_path', lambda: w['replica'])
    monkeypatch.setattr(activity, '_read_remote_links', lambda *a, **kw: [])
    rows = activity._legacy_remote_links()
    assert len(rows) == 1 and rows[0]['released'] is True


def test_failed_inventory_read_revalidates_retained_proof(world, monkeypatch):
    from hermes_cli import kanban_session_activity as activity
    w = world
    register(w)
    released = carry.reconcile_carry([w['card']])[0]
    w['owner'].unlink()
    monkeypatch.setattr(activity, '_remote_last', None)
    monkeypatch.setattr(activity, '_remote_snapshot', lambda cards=None: [released])
    monkeypatch.setattr(activity, '_hub_db_path', lambda: None)
    assert activity._legacy_remote_links()[0]['released'] is False


@pytest.mark.parametrize('value', [[], None, {'schema': 1, 'identity': 'x'}, {'schema': 1, 'identity': []}])
def test_malformed_receipts_do_not_break_inventory(world, value):
    world['receipt'].parent.mkdir(parents=True)
    world['receipt'].write_text(json.dumps(value))
    assert carry.reconcile_carry([world['card']]) == [world['card']]


@pytest.mark.parametrize('error', [FileNotFoundError('missing'), ValueError('invalid')])
def test_missing_origin_identity_fails_closed(world, monkeypatch, error):
    from hermes_cli import kanban_origin_adapter
    register(world)
    released = carry.reconcile_carry([world['card']])[0]
    def fail(path):
        raise error
    monkeypatch.setattr(kanban_origin_adapter, '_gateway', fail)
    assert carry.reconcile_carry([released])[0]['released'] is False


@pytest.mark.parametrize('wal', [b'', b'nonempty'])
def test_snapshot_wal_policy(world, wal):
    from pathlib import Path
    Path(str(world['owner']) + '-wal').write_bytes(wal)
    if wal:
        with pytest.raises(ValueError, match='live_database'):
            register(world)
    else:
        register(world)
        assert carry.reconcile_carry([world['card']])[0]['released'] is True


def test_board_path_traversal_refused(world):
    card = {**world['card'], 'board': '../default', 'key': 'owner/default/../default/t_1'}
    with pytest.raises(ValueError, match='invalid_identity'):
        register(world, card=card)


def test_receiptless_goal_mode_never_uses_other_tasks_judge_epoch(world):
    for path in (world['owner'], world['replica']):
        edit(path, 'UPDATE tasks SET goal_mode=1')
        edit(path, "INSERT INTO task_events VALUES(1000,'other_task',8,'goal_verified',200,'{}','unrelated-judge')")
    with pytest.raises(ValueError, match='completion_unverified'):
        register(world)
    assert not world['receipt'].exists()


@pytest.mark.parametrize('failure', ['json', 'shape', 'missing_shadow', 'corrupt_shadow',
                                     'nested_json', 'schema_skew'])
def test_storage_failure_after_release_restores_unknown(world, failure):
    register(world)
    released = carry.reconcile_carry([world['card']])[0]
    assert released['released'] is True
    def nested_json():
        # Python 3.14's decoder uses a C-stack budget, not sys.getrecursionlimit().
        depth = 100000
        world['receipt'].write_text('[' * depth + '0' + ']' * depth)

    mutations = {
        'json': lambda: world['receipt'].write_text('{'),
        'shape': lambda: world['receipt'].write_text('[]'),
        'missing_shadow': world['replica'].unlink,
        'corrupt_shadow': lambda: world['replica'].write_bytes(b'not sqlite'),
        'nested_json': nested_json,
        'schema_skew': lambda: edit(world['replica'], 'ALTER TABLE tasks DROP COLUMN body'),
    }
    mutations[failure]()
    row = carry.reconcile_carry([released])[0]
    assert (row['status'], row['unfinished'], row['released']) == ('unknown', True, False)


def test_fresh_owner_reopen_supersedes_verified_snapshot(world, monkeypatch):
    from hermes_cli import kanban_session_activity as activity
    register(world)
    released = carry.reconcile_carry([world['card']])[0]
    fresh = {**world['card'], 'stale': False, 'status': 'ready', 'reported_status': 'ready'}
    monkeypatch.setattr(activity, '_remote_last', None)
    monkeypatch.setattr(activity, '_snapshot_saved', None)
    monkeypatch.setattr(activity, '_remote_snapshot', lambda cards=None: [released] if cards is None else cards)
    monkeypatch.setattr(activity, '_hub_db_path', lambda: world['replica'])
    monkeypatch.setattr(activity, '_read_remote_links', lambda *a, **kw: [fresh])
    assert activity._legacy_remote_links() == [fresh]


def test_standard_reconciler_captures_release_and_restores_on_reopen(world, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc, kanban_chat_links as links
    from hermes_cli import kanban_sync_client as sync, kanban_session_activity as activity
    from tests.hermes_cli import kanban_sync_support as support
    w = world
    home = w['owner'].parent
    monkeypatch.setenv('HERMES_HOME', str(home))
    support.write_sync_config(home, True)
    conn = kbc.connect(home / 'live.db')
    sync.ensure_identity(conn, 'default', 'origin')
    monkeypatch.setattr(activity, 'remote_links_and_origin_rows',
                        lambda: (carry.reconcile_carry([w['card']]), []))
    try:
        links.reconcile(conn, 'default', 'origin')
        assert conn.execute("SELECT released FROM task_chat_links WHERE kind='shepherd'").fetchone()[0] == 0
        register(w)
        assert links.reconcile(conn, 'default', 'origin')['changed'] == 1
        assert conn.execute("SELECT released FROM task_chat_links WHERE kind='shepherd'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM kanban_sync_outbox WHERE table_name='task_chat_links'").fetchone()[0] >= 2
        edit(w['replica'], "UPDATE tasks SET status='ready'")
        assert links.reconcile(conn, 'default', 'origin')['changed'] == 1
        assert conn.execute("SELECT released FROM task_chat_links WHERE kind='shepherd'").fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    finally:
        conn.close()
