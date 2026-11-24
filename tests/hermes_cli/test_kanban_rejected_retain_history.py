"""Historical custody is archival only; no restored command or lifecycle projection."""
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.hermes_cli import test_kanban_rejected_retain_source as source_tests


@pytest.fixture
def case(tmp_path):
    from hermes_cli import kanban_rejected_retain_history as history
    path, command, wire, *_ = source_tests.fixture_db(tmp_path)
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE tasks(id TEXT PRIMARY KEY, current_run_id INTEGER);
        CREATE TABLE task_events(uid TEXT PRIMARY KEY);
        CREATE TABLE task_runs(id INTEGER PRIMARY KEY, uid TEXT, task_id TEXT, ended_at INTEGER);
        CREATE TABLE kanban_sync_outbox(op_id TEXT PRIMARY KEY,table_name TEXT,row_uid TEXT,
            op TEXT,fields TEXT,state TEXT,last_error TEXT);
        CREATE TABLE kanban_sync_tombstones(table_name TEXT,row_uid TEXT,deleted_at INTEGER);''')
    op = dict(op_id=command['op_id'], table_name='tasks', row_uid=wire['task'], op='retain',
              fields=command['request_json'], state='rejected', last_error='missing_decision_episode')
    db.execute('INSERT INTO kanban_sync_outbox VALUES (?,?,?,?,?,?,?)', tuple(op.values()))
    db.commit()
    rows = {
        ('tasks', wire['task']): dict(uid=wire['task'], status='done', current_run_uid=None, lease_run_uid=None),
        ('task_runs', wire['run_uid']): dict(uid=wire['run_uid'], task_id=wire['task'], ended_at=101, status='blocked'),
    }
    def evidence(board, table, uid):
        row = rows.get((table, uid))
        return dict(board=board, table=table, uid=uid, head=123, floor=0,
                    state='live' if row else 'never_existed', row=row, tombstone=None)
    return history, db, op, wire, path, rows, SimpleNamespace(row_evidence=evidence)


def test_archival_preserves_source_wire_rejection_and_never_backfills(case):
    history, db, op, _wire, path, _, hub = case
    before = path.read_bytes()
    report = history.retire(db, hub, 'default', op, path)
    assert report['action'] == 'retired', report
    assert report['classification'] == 'historical_rejected_source'
    assert not db.execute('SELECT * FROM kanban_sync_outbox').fetchall()
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_failure_commands'").fetchone()
    assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 0
    receipt = dict(db.execute('SELECT * FROM kanban_rejected_retain_history_receipts').fetchone())
    assert hashlib.sha256(receipt['snapshot_json'].encode()).hexdigest() == receipt['snapshot_sha256']
    snapshot = json.loads(receipt['snapshot_json'])
    assert snapshot['operation'] == op
    assert snapshot['lifecycle_authority'] is False
    assert snapshot['source_encoding'] == 'typed-json-v1'
    assert path.read_bytes() == before
    for sql in ['DELETE FROM kanban_rejected_retain_history_receipts',
                "UPDATE kanban_rejected_retain_history_receipts SET snapshot_sha256='bad'",
                'INSERT OR REPLACE INTO kanban_rejected_retain_history_receipts SELECT * FROM kanban_rejected_retain_history_receipts']:
        with pytest.raises(sqlite3.IntegrityError, match='rejected_retain_history_immutable'):
            db.execute(sql)
        db.rollback()


@pytest.mark.parametrize('defect', ['local_task','local_run','pending_task','pending_run','tomb_task','tomb_run',
    'canonical_active','canonical_lease','run_open','run_foreign','run_retained','event_live','event_pruned',
    'event_tomb','event_identity','head_bool','wrong_board','source_missing','source_changed','task_race','op_race'])
def test_refuses_adjacent_invalid_or_changed_evidence(case, defect):
    history, db, op, _wire, path, rows, hub = case
    if defect == 'local_task': db.execute("INSERT INTO tasks VALUES ('task-1',NULL)")
    if defect == 'local_run': db.execute("INSERT INTO task_runs VALUES (1,'run-1','task-1',1)")
    if defect.startswith('pending_'):
        table, uid = ('tasks','task-1') if defect.endswith('task') else ('task_runs','run-1')
        db.execute('INSERT INTO kanban_sync_outbox VALUES (?,?,?,?,?,?,?)', ('pending',table,uid,'upsert','{}','pending',None))
    if defect.startswith('tomb_'):
        table, uid = ('tasks','task-1') if defect.endswith('task') else ('task_runs','run-1')
        db.execute('INSERT INTO kanban_sync_tombstones VALUES (?,?,1)', (table,uid))
    if defect == 'canonical_active': rows['tasks','task-1']['status'] = 'running'
    if defect == 'canonical_lease': rows['tasks','task-1']['lease_run_uid'] = 'successor'
    if defect == 'run_open': rows['task_runs','run-1']['ended_at'] = None
    if defect == 'run_foreign': rows['task_runs','run-1']['task_id'] = 'foreign'
    if defect == 'run_retained': rows['task_runs','run-1']['status'] = 'retained'
    if defect == 'event_live': rows['task_events','event-1'] = dict(uid='event-1',kind='decision_retained')
    if defect == 'source_missing': path = path.parent / 'absent.db'
    db.commit()
    original = hub.row_evidence
    def evidence(*args):
        r = original(*args)
        if args[1] == 'task_events':
            if defect == 'event_pruned': r['floor'] = 1
            if defect == 'event_tomb': r['tombstone'] = {'uid':'event-1'}
            if defect == 'event_identity': r['uid'] = 'foreign'
        if defect == 'head_bool': r['head'] = True
        if defect == 'wrong_board': r['board'] = 'foreign'
        if defect == 'source_changed':
            with sqlite3.connect(path) as state: state.execute("UPDATE session_admissions SET status='active'")
        if defect == 'task_race':
            db.execute("INSERT OR IGNORE INTO tasks VALUES ('task-1',NULL)"); db.commit()
        if defect == 'op_race':
            db.execute("UPDATE kanban_sync_outbox SET last_error='changed'"); db.commit()
        return r
    hub.row_evidence = evidence
    report = history.retire(db, hub, 'default', op, path)
    assert report['action'] == 'retained', report
    assert db.execute('SELECT count(*) FROM kanban_sync_outbox WHERE op_id=?',(op['op_id'],)).fetchone()[0] == 1
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_rejected_retain_history_receipts'").fetchone()


def test_normal_resolver_uses_historical_contract_only_when_live_custody_is_missing(case, monkeypatch):
    from hermes_cli import kanban_sync_rejections as resolver
    from hermes_cli import kanban_rejected_retain_retirement as current
    import hermes_constants
    _history, db, _op, _wire, path, _, hub = case
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: path.parent)
    monkeypatch.setattr(current, 'retire', lambda *a: dict(action='retained', classification='unresolved'))
    reports = resolver.resolve_rejections(db, hub, 'default')
    assert reports[0]['action'] == 'retired', reports
    assert db.execute('SELECT count(*) FROM kanban_rejected_retain_history_receipts').fetchone()[0] == 1


@pytest.mark.parametrize('table,key,value', [
    ('tasks', 'uid', 'foreign'), ('tasks', 'status', 'archived'),
    ('tasks', 'current_run_uid', 'new-run'), ('tasks', 'lease_run_uid', 'new-run'),
    ('task_runs', 'uid', 'foreign'), ('task_runs', 'task_id', 'foreign'),
    ('task_runs', 'ended_at', 0), ('task_runs', 'ended_at', True),
    ('task_runs', 'ended_at', 101.0), ('task_runs', 'status', 'retained'),
])
def test_canonical_row_guards(case, table, key, value):
    history, db, op, wire, path, rows, hub = case
    uid = wire['task'] if table == 'tasks' else wire['run_uid']
    rows[table, uid][key] = value
    assert history.retire(db, hub, 'default', op, path)['action'] == 'retained'


@pytest.mark.parametrize('key', ['current_run_uid', 'lease_run_uid'])
def test_missing_off_lease_observation_is_not_null(case, key):
    history, db, op, wire, path, rows, hub = case
    del rows['tasks', wire['task']][key]
    assert history.retire(db, hub, 'default', op, path)['action'] == 'retained'


@pytest.mark.parametrize('column', ['task_id', 'run_uid', 'op_id', 'event_uid'])
def test_any_remaining_current_command_custody_vetoes_history(case, column):
    history, db, op, wire, path, _rows, hub = case
    db.execute('CREATE TABLE kanban_failure_commands(task_id TEXT, run_uid TEXT, op_id TEXT, event_uid TEXT)')
    value = {'task_id':wire['task'], 'run_uid':wire['run_uid'], 'op_id':op['op_id'], 'event_uid':wire['event_uid']}[column]
    db.execute(f'INSERT INTO kanban_failure_commands ({column}) VALUES (?)', (value,))
    db.commit()
    assert history.retire(db, hub, 'default', op, path)['action'] == 'retained'


def test_source_blob_archive_is_lossless_and_collision_free(case):
    history, *_ = case
    value = {'bytes': b'\x00\xff', 'tag-shaped': ['bytes', 'AP8=']}
    encoded = history._typed(value)
    def decode(node):
        import base64
        tag, data = node
        if tag == 'bytes': return base64.b64decode(data)
        if tag == 'dict': return {k:decode(v) for k,v in data}
        if tag == 'list': return [decode(v) for v in data]
        return data
    assert decode(encoded) == value
    assert history._typed(b'\x00\xff') != history._typed(['bytes', 'AP8='])


