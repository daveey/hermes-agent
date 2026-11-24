"""Append-only host custody is historical observation, never current authority."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_history_residue_custody as custody
from hermes_cli.kanban_history_residue_source import Refused
from tests.hermes_cli.test_kanban_history_residue_proof import proof


SCOPE = ('https://board.example', 'default', 'home')


def request():
    raw, data = proof()
    return json.dumps({'version': 1, 'task': 't_stopped', 'expected_version': 7,
                       'source_json': raw, 'reason': 'Keep history'}), data['receipt']


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / 'custody.db', isolation_level=None)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_stage_seal_reopen_preserves_exact_bytes_and_never_supplies_current_authority(db):
    raw, receipt = request()
    custody.stage(db, SCOPE, raw)
    custody.seal(db, SCOPE, 'op-original', json.dumps(receipt, indent=2))
    path = db.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(path) as other:
        other.row_factory = sqlite3.Row
        saved = custody.lookup(other, SCOPE, 'op-original')
        assert saved == {'request_json': raw, 'receipt_json': json.dumps(receipt, indent=2)}
        assert 'current' not in saved
        assert custody.lookup(other, (SCOPE[0], 'other', 'home'), 'op-original') is None
        assert custody.lookup(other, (SCOPE[0], 'default', 'other'), 'op-original') is None
        assert custody.lookup(other, ('https://other', 'default', 'home'), 'op-original') is None


def test_identical_retries_are_noop_but_changed_bytes_conflict(db):
    raw, receipt = request()
    custody.stage(db, SCOPE, raw)
    custody.seal(db, SCOPE, 'op-original', json.dumps(receipt))
    before = db.total_changes
    custody.stage(db, SCOPE, raw)
    custody.seal(db, SCOPE, 'op-original', json.dumps(receipt))
    assert db.total_changes == before
    with pytest.raises(Refused, match='conflict'):
        custody.stage(db, SCOPE, raw + ' ')
    with pytest.raises(Refused, match='conflict'):
        custody.seal(db, SCOPE, 'op-original', json.dumps(receipt) + ' ')


@pytest.mark.parametrize('table', ['kanban_history_residue_intents', 'kanban_history_residue_receipts'])
@pytest.mark.parametrize('mutation', ['UPDATE', 'DELETE', 'REPLACE'])
def test_original_custody_cannot_be_updated_deleted_or_replaced(db, table, mutation):
    raw, receipt = request()
    custody.stage(db, SCOPE, raw)
    custody.seal(db, SCOPE, 'op-original', json.dumps(receipt))
    before = [tuple(r) for r in db.execute(f'SELECT * FROM {table}')]
    query = {'UPDATE': f'UPDATE {table} SET op_id=op_id', 'DELETE': f'DELETE FROM {table}',
             'REPLACE': f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}'}[mutation]
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        db.execute(query)
    assert [tuple(r) for r in db.execute(f'SELECT * FROM {table}')] == before


@pytest.mark.parametrize('change', ['source', 'reason', 'revision', 'task', 'authority', 'gateway'])
def test_receipt_must_match_frozen_request_not_just_operation_id(db, change):
    raw, receipt = request()
    custody.stage(db, SCOPE, raw)
    if change == 'source': receipt['source_json'] += ' '
    if change == 'reason': receipt['reason'] += ' changed'
    if change == 'revision': receipt['parent']['version'] += 1
    if change == 'task': receipt['parent']['uid'] = 'different'
    if change == 'authority': receipt['canonical_acceptance'] = True
    if change == 'gateway': receipt['gateway'] = 'different'
    with pytest.raises(Refused):
        custody.seal(db, SCOPE, 'op-original', json.dumps(receipt))
    assert custody.lookup(db, SCOPE, 'op-original')['receipt_json'] is None


def test_uncertain_delivery_retains_intent_without_manufacturing_receipt(db):
    raw, _ = request()
    custody.stage(db, SCOPE, raw)
    assert custody.lookup(db, SCOPE, 'op-original') == {'request_json': raw, 'receipt_json': None}
    with pytest.raises(Refused, match='intent_missing'):
        custody.seal(db, SCOPE, 'another-op', '{}')


def test_seal_rollback_keeps_original_intent(db):
    raw, receipt = request()
    custody.stage(db, SCOPE, raw)
    db.execute("CREATE TRIGGER fail_receipt AFTER INSERT ON kanban_history_residue_receipts BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        custody.seal(db, SCOPE, 'op-original', json.dumps(receipt))
    assert custody.lookup(db, SCOPE, 'op-original')['receipt_json'] is None


@pytest.mark.parametrize('change', ['version', 'revision', 'task', 'reason', 'unknown', 'duplicate'])
def test_bad_request_is_refused_without_installing_any_schema(db, change):
    raw, _ = request()
    obj = json.loads(raw)
    if change == 'version': obj['version'] = True
    if change == 'revision': obj['expected_version'] = 0
    if change == 'task': obj['task'] = 'different'
    if change == 'reason': obj['reason'] = ''
    if change == 'unknown': obj['execution_authority'] = True
    raw = json.dumps(obj)
    if change == 'duplicate': raw = raw[:-1] + ',"version":1}'
    before = db.execute('SELECT * FROM sqlite_master').fetchall()
    with pytest.raises(Refused):
        custody.stage(db, SCOPE, raw)
    assert db.execute('SELECT * FROM sqlite_master').fetchall() == before


@pytest.mark.parametrize('action', ['stage', 'seal'])
def test_custody_writes_require_their_own_commit_boundary(db, action):
    raw, receipt = request()
    custody.stage(db, SCOPE, raw)
    db.execute('BEGIN')
    try:
        with pytest.raises(Refused, match='nested_transaction'):
            if action == 'stage': custody.stage(db, SCOPE, raw)
            else: custody.seal(db, SCOPE, 'op-original', json.dumps(receipt))
        assert db.in_transaction
    finally:
        db.rollback()


def test_same_event_under_changed_op_identity_cannot_replace_intent(db):
    raw, _ = request()
    custody.stage(db, SCOPE, raw)
    obj = json.loads(raw)
    source = json.loads(obj['source_json'])
    source['op_id'] = 'new-op'
    wire = json.loads(source['wire_json'])
    wire['client_op_id'] = 'new-op'
    source['wire_json'] = json.dumps(wire)
    obj['source_json'] = json.dumps(source)
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        custody.stage(db, SCOPE, json.dumps(obj))
    assert custody.lookup(db, SCOPE, 'op-original')['request_json'] == raw
    assert custody.lookup(db, SCOPE, 'new-op') is None

