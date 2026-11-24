"""Rejected retain transport may retire, never its immutable rejection custody."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from tests.hermes_cli import test_kanban_sync_rejections as resolver_tests
from tests.hermes_cli import test_kanban_sync_retain_drift as drift_tests

kanban_home = resolver_tests.kanban_home
live = resolver_tests.live
svc = resolver_tests.svc
stores = drift_tests.stores  # Export the defining module's fixture for pytest discovery.


@pytest.fixture
def case(stores):
    live, _, op = stores
    live.execute('ALTER TABLE kanban_failure_commands ADD COLUMN rejected_release_op_id TEXT')
    live.execute('CREATE TABLE kanban_sync_outbox(op_id TEXT PRIMARY KEY, table_name TEXT, row_uid TEXT, '
                 'op TEXT, state TEXT, last_error TEXT, fields TEXT)')
    live.execute('INSERT INTO kanban_sync_outbox VALUES (?,?,?,?,?,?,?)',
                 tuple(op[k] for k in ('op_id', 'table_name', 'row_uid', 'op', 'state', 'last_error', 'fields')))
    live.execute('CREATE TABLE kanban_sync_tombstones(table_name TEXT, row_uid TEXT, deleted_at INTEGER)')
    live.commit()
    rows = {
        ('tasks', 't_card'): dict(uid='t_card', current_run_uid=None, lease_run_uid=None),
        ('task_runs', 'run'): dict(uid='run', task_id='t_card', ended_at=123),
    }

    def evidence(board, table, uid):
        return dict(board=board, table=table, uid=uid, head=123, state='live', row=rows[table, uid])

    return live, op, rows, SimpleNamespace(row_evidence=evidence)


def test_retirement_keeps_exact_rejection_and_command_custody(case):
    from hermes_cli.kanban_sync_rejections import resolve_rejections

    live, op, _, hub = case
    commands = [tuple(r) for r in live.execute('SELECT * FROM kanban_failure_commands')]
    assert resolve_rejections(live, hub, 'default')[0]['action'] == 'retired'
    assert live.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0] == 0
    assert [tuple(r) for r in live.execute('SELECT * FROM kanban_failure_commands')] == commands
    receipt = dict(live.execute('SELECT * FROM kanban_rejected_retain_receipts').fetchone())
    snapshot = json.loads(receipt['snapshot_json'])
    assert snapshot['operation'] == op
    assert receipt['snapshot_sha256'] == hashlib.sha256(receipt['snapshot_json'].encode()).hexdigest()
    assert snapshot['command']['delivery_state'] == 'rejected'
    import sqlite3
    for statement in ('DELETE FROM kanban_rejected_retain_receipts',
                      "UPDATE kanban_rejected_retain_receipts SET snapshot_sha256='changed'",
                      "INSERT OR REPLACE INTO kanban_rejected_retain_receipts "
                      "SELECT op_id, '{}', 'changed', '{}' FROM kanban_rejected_retain_receipts"):
        with pytest.raises(sqlite3.IntegrityError, match='rejected_retain_receipt_immutable'):
            live.execute(statement)
    live.rollback()
    from hermes_cli.kanban_retain_transport import outbound
    with pytest.raises(ValueError, match='immutable custody retained'):
        outbound(live, op)
    assert resolve_rejections(live, hub, 'default') == []


@pytest.mark.parametrize('veto', [
    'missing_custody', 'unknown', 'receipt', 'digest', 'different_request', 'fallback',
    'local_open', 'local_current', 'pending_task', 'pending_run', 'task_tomb', 'run_tomb',
    'remote_open', 'remote_current', 'remote_lease', 'foreign_run', 'identity', 'board',
    'head', 'unavailable', 'custody_race', 'task_race', 'operation_race',
])
def test_refuses_incomplete_proof_without_transport_or_lifecycle_changes(case, veto):
    from hermes_cli.kanban_rejected_retain_retirement import retire

    live, op, rows, hub = case
    statements = {
        'missing_custody': 'DELETE FROM kanban_failure_commands',
        'unknown': "UPDATE kanban_failure_commands SET delivery_state='unknown'",
        'receipt': "UPDATE kanban_failure_commands SET canonical_receipt_json='{}'",
        'digest': "UPDATE kanban_failure_commands SET request_sha256='bad'",
        'different_request': "UPDATE kanban_failure_commands SET request_uid='different'",
        'fallback': "UPDATE kanban_failure_commands SET rejected_release_op_id='release'",
        'local_open': 'UPDATE task_runs SET ended_at=NULL',
        'local_current': 'UPDATE tasks SET current_run_id=1',
    }
    if veto in statements:
        live.execute(statements[veto])
    if veto in ('pending_task', 'pending_run'):
        table, uid = ('tasks', 't_card') if veto == 'pending_task' else ('task_runs', 'run')
        live.execute('INSERT INTO kanban_sync_outbox VALUES (?,?,?,?,?,?,?)',
                     ('pending', table, uid, 'upsert', 'pending', None, '{}'))
    if veto in ('task_tomb', 'run_tomb'):
        table, uid = ('tasks', 't_card') if veto == 'task_tomb' else ('task_runs', 'run')
        live.execute('INSERT INTO kanban_sync_tombstones VALUES (?,?,1)', (table, uid))
    if veto == 'remote_open':
        rows['task_runs', 'run']['ended_at'] = None
    if veto in ('remote_current', 'remote_lease'):
        rows['tasks', 't_card']['current_run_uid' if veto == 'remote_current' else 'lease_run_uid'] = 'run'
    if veto == 'foreign_run':
        rows['task_runs', 'run']['task_id'] = 'other'
    live.commit()
    original = hub.row_evidence

    def evidence(*args):
        if veto == 'unavailable':
            raise OSError('offline')
        result = original(*args)
        if veto in ('identity', 'board', 'head'):
            result[dict(identity='uid', board='board', head='head')[veto]] = 'wrong'
        race = {
            'custody_race': "UPDATE kanban_failure_commands SET delivery_state='unknown'",
            'task_race': 'UPDATE tasks SET current_run_id=1',
            'operation_race': "UPDATE kanban_sync_outbox SET last_error='changed'",
        }
        if veto in race:
            live.execute(race[veto])
            live.commit()
        return result

    hub.row_evidence = evidence
    before = [tuple(r) for r in live.execute('SELECT * FROM kanban_sync_outbox')]
    assert retire(live, hub, 'default', op)['action'] == 'retained'
    after = [tuple(r) for r in live.execute('SELECT * FROM kanban_sync_outbox')]
    if veto != 'operation_race':
        assert after == before
    assert live.execute('SELECT COUNT(*) FROM kanban_sync_outbox WHERE op_id=\'op\'').fetchone()[0] == 1
    assert not live.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_rejected_retain_receipts'").fetchone()


def test_successor_fence_is_not_projected_or_cleared(case):
    from hermes_cli.kanban_rejected_retain_retirement import retire

    live, op, rows, hub = case
    for key in ('current_run_uid', 'lease_run_uid'):
        rows['tasks', 't_card'][key] = 'successor'
    live.execute('ALTER TABLE tasks ADD COLUMN claim_lock TEXT')
    live.execute("UPDATE tasks SET claim_lock='successor-lock'")
    live.commit()
    task = dict(live.execute('SELECT * FROM tasks').fetchone())
    assert retire(live, hub, 'default', op)['action'] == 'retired'
    assert dict(live.execute('SELECT * FROM tasks').fetchone()) == task


def test_conflicting_archive_is_never_replaced(case):
    from hermes_cli.kanban_rejected_retain_retirement import retire

    live, op, _, hub = case
    assert retire(live, hub, 'default', op)['action'] == 'retired'
    archived = dict(live.execute('SELECT * FROM kanban_rejected_retain_receipts').fetchone())
    live.execute('INSERT INTO kanban_sync_outbox VALUES (?,?,?,?,?,?,?)',
                 tuple(op[k] for k in ('op_id', 'table_name', 'row_uid', 'op', 'state', 'last_error', 'fields')))
    live.commit()
    assert retire(live, hub, 'default', op)['action'] == 'retired'
    assert dict(live.execute('SELECT * FROM kanban_rejected_retain_receipts').fetchone()) == archived
    altered = {**op, 'extra_legacy_metadata': 'different'}
    # Transport shape remains unchanged; any snapshot conflict fails closed.
    live.execute('ALTER TABLE kanban_sync_outbox ADD COLUMN extra_legacy_metadata TEXT')
    live.execute('INSERT INTO kanban_sync_outbox VALUES (?,?,?,?,?,?,?,?)',
                 tuple(altered[k] for k in ('op_id', 'table_name', 'row_uid', 'op', 'state', 'last_error', 'fields',
                                           'extra_legacy_metadata')))
    live.commit()
    assert retire(live, hub, 'default', altered)['action'] == 'retained'
    assert dict(live.execute('SELECT * FROM kanban_rejected_retain_receipts').fetchone()) == archived
    assert live.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0] == 1


def test_command_removed_between_preflight_and_snapshot_is_not_a_sync_fault(case, monkeypatch):
    from hermes_cli import kanban_rejected_retain_retirement as retirement

    live, op, _, hub = case
    original = retirement._snapshot

    def vanished(*args):
        live.execute('DELETE FROM kanban_failure_commands')
        live.commit()
        return original(*args)

    monkeypatch.setattr(retirement, '_snapshot', vanished)
    assert retirement.retire(live, hub, 'default', op)['action'] == 'retained'
    assert live.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0] == 1


def test_normal_sync_archives_with_production_schema_and_evidence_envelope(live, svc, kanban_home):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_failure_release as release
    from hermes_cli import kanban_sync_client as client

    hub = svc.hub('macbook')
    tid = kb.create_task(live, title='ended retain rejection', assignee='worker')
    client.push(live, hub, 'default', 'macbook', primary=False)
    sync.set_mode(live, 'primary')
    wire = dict(op='retain', client_op_id='rejected-retain', task=tid, run_uid='ended-run', event_uid='reserved-event')
    text = json.dumps(wire, sort_keys=True, separators=(',', ':'))
    with kb.write_txn(live):
        release._ensure_commands(live)
        live.execute('UPDATE kanban_sync_state SET applying=1')
        live.execute("INSERT INTO task_runs(uid,task_id,status,started_at,ended_at,sync_version) "
                     "VALUES ('ended-run',?,'released',1,2,1)", (tid,))
        live.execute('UPDATE kanban_sync_state SET applying=0')
        live.execute('INSERT INTO kanban_failure_commands '
                     '(run_uid,task_id,op_id,operation_kind,request_uid,event_uid,request_json,request_sha256,delivery_state) '
                     "VALUES ('ended-run',?,'rejected-retain','retain','rejected-retain','reserved-event',?,?,'rejected')",
                     (tid, text, hashlib.sha256(text.encode()).hexdigest()))
        live.execute('INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at,state,last_error) '
                     "VALUES ('rejected-retain','tasks',?,'retain',?,'',1,'rejected','missing_decision_episode')", (tid, text))
    svc.board('default')['rows'].setdefault('task_runs', {})['ended-run'] = dict(
        uid='ended-run', task_id=tid, version=1, status='released', started_at=1, ended_at=2)
    result = client.sync_board('default', kanban_home / 'kanban.db', hub, 'macbook')
    report = next(r for r in result.rejection_resolutions if r['op_id'] == 'rejected-retain')
    assert report['action'] == 'retired', report
    assert client.status(live)['rejected'] == 0
    receipt = dict(live.execute('SELECT * FROM kanban_rejected_retain_receipts').fetchone())
    assert json.loads(receipt['snapshot_json'])['operation']['last_error'] == 'missing_decision_episode'
