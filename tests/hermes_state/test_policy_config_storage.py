"""Exact-byte frozen configuration storage; no production activation in this test."""
import json
import sqlite3
from contextlib import closing

import pytest

from hermes_state_policy_config import (
    BLOB_PREFIX, REF_PREFIX, delete_receipt, load_receipt, store_receipt,
)
from hermes_state_runtime import RuntimeStoreError


@pytest.fixture
def conn():
    with closing(sqlite3.connect(':memory:')) as value:
        value.execute('CREATE TABLE state_meta(key TEXT PRIMARY KEY, value TEXT)')
        value.execute('BEGIN')
        yield value


def receipt(config=' {"unicode": "雪", "a": 1} \n'):
    return {'session_id': 'one', 'policy': {'config_json': config, 'request_json': '{ "raw": true }'}}


def inventory(conn, prefix):
    return conn.execute('SELECT key,value FROM state_meta WHERE key GLOB ?', (prefix + '*',)).fetchall()


def test_default_is_legacy_inline_without_auxiliary_writes(conn):
    expected = receipt()
    store_receipt(conn, 'one', expected)
    raw = conn.execute('SELECT value FROM state_meta').fetchone()[0]
    assert json.loads(raw) == expected
    assert inventory(conn, BLOB_PREFIX) == inventory(conn, REF_PREFIX) == []


def test_exact_bytes_shared_and_last_reference_retirement(conn):
    expected = receipt()
    original = json.loads(json.dumps(expected))
    store_receipt(conn, 'one', expected, intern=True)
    store_receipt(conn, 'two', expected, intern=True)
    assert expected == original
    assert len(inventory(conn, BLOB_PREFIX)) == 1
    assert len(inventory(conn, REF_PREFIX)) == 2
    assert load_receipt(conn, 'one') == load_receipt(conn, 'two') == expected
    raw = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', ('gateway.local_policy.v1:one',)).fetchone()[0])
    assert 'config_json' not in raw['policy']
    assert raw['policy']['request_json'] == expected['policy']['request_json']
    delete_receipt(conn, 'one')
    assert load_receipt(conn, 'two') == expected
    assert len(inventory(conn, BLOB_PREFIX)) == 1
    delete_receipt(conn, 'two')
    assert inventory(conn, BLOB_PREFIX) == inventory(conn, REF_PREFIX) == []


def test_byte_distinct_json_not_normalized(conn):
    for sid, text in [('a', '{"a":1}'), ('b', '{ "a": 1 }')]:
        store_receipt(conn, sid, receipt(text), intern=True)
        assert load_receipt(conn, sid)['policy']['config_json'] == text
    assert len(inventory(conn, BLOB_PREFIX)) == 2


def test_replacement_and_inline_rollback_drop_only_unreferenced_blob(conn):
    store_receipt(conn, 'one', receipt(), intern=True)
    store_receipt(conn, 'two', receipt(), intern=True)
    store_receipt(conn, 'one', receipt('new'), intern=True)
    assert len(inventory(conn, BLOB_PREFIX)) == 2
    store_receipt(conn, 'two', load_receipt(conn, 'two'))
    assert len(inventory(conn, BLOB_PREFIX)) == 1
    assert load_receipt(conn, 'one')['policy']['config_json'] == 'new'
    store_receipt(conn, 'one', load_receipt(conn, 'one'))
    assert inventory(conn, BLOB_PREFIX) == inventory(conn, REF_PREFIX) == []


@pytest.mark.parametrize('corrupt', [None, 'wrong bytes'])
def test_missing_or_corrupt_blob_fails_closed_without_changes(conn, corrupt):
    store_receipt(conn, 'one', receipt(), intern=True)
    key = inventory(conn, BLOB_PREFIX)[0][0]
    if corrupt is None:
        conn.execute('DELETE FROM state_meta WHERE key=?', (key,))
    else:
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (corrupt, key))
    before = conn.execute('SELECT * FROM state_meta ORDER BY key').fetchall()
    with pytest.raises(RuntimeStoreError, match='storage_unavailable'):
        load_receipt(conn, 'one')
    assert conn.execute('SELECT * FROM state_meta ORDER BY key').fetchall() == before


def test_blob_and_reference_rollback_with_receipt_transaction(conn):
    conn.commit()
    with pytest.raises(RuntimeError, match='injected'):
        with conn:
            conn.execute('BEGIN')
            store_receipt(conn, 'one', receipt(), intern=True)
            raise RuntimeError('injected')
    assert conn.execute('SELECT * FROM state_meta').fetchall() == []


def test_legacy_decoder_boundary_is_explicit(conn):
    store_receipt(conn, 'one', receipt(), intern=True)
    raw = conn.execute('SELECT value FROM state_meta WHERE key=?', ('gateway.local_policy.v1:one',)).fetchone()[0]
    with pytest.raises(KeyError, match='config_json'):
        json.loads(raw)['policy']['config_json']
    assert load_receipt(conn, 'one') == receipt()


def test_failed_receipt_write_rolls_back_blob_and_reference_only(conn):
    store_receipt(conn, 'one', receipt(), intern=True)
    before = conn.execute('SELECT * FROM state_meta ORDER BY key').fetchall()
    conn.execute("CREATE TRIGGER fail_receipt BEFORE UPDATE ON state_meta "
                 "WHEN NEW.key='gateway.local_policy.v1:one' "
                 "BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        store_receipt(conn, 'one', receipt('replacement'), intern=True)
    assert conn.in_transaction
    assert conn.execute('SELECT * FROM state_meta ORDER BY key').fetchall() == before
    assert load_receipt(conn, 'one') == receipt()


def test_no_owner_transaction_refuses_writes(conn):
    conn.commit()
    for operation in (lambda: store_receipt(conn, 'one', receipt(), intern=True),
                      lambda: delete_receipt(conn, 'one')):
        with pytest.raises(RuntimeStoreError, match='runtime_coordination_required'):
            operation()
        assert not conn.in_transaction
        assert conn.execute('SELECT * FROM state_meta').fetchall() == []


@pytest.mark.parametrize('digest', ['bad', 'A' * 64, {}, 1])
def test_invalid_reference_refused(conn, digest):
    from hermes_state_policy_config import decode_receipt
    raw = json.dumps({'policy': {'config_ref_v1': digest}})
    with pytest.raises(RuntimeStoreError, match='storage_unavailable'):
        decode_receipt(conn, raw)


def test_legacy_invalid_policy_stays_at_existing_validation_boundary(conn):
    from hermes_state_policy_config import decode_receipt
    for value in ({}, {'policy': None}, {'policy': {}}, {'policy': []}):
        assert decode_receipt(conn, json.dumps(value)) == value


def test_dbstat_pages_reclaimed_logically_without_vacuum(conn, record_property):
    text = ' {"frozen": "' + 'x' * 65536 + '"} '
    for number in range(40):
        store_receipt(conn, str(number), receipt(text))
    before = conn.execute("SELECT sum(pgsize) FROM dbstat WHERE name='state_meta'").fetchone()[0]
    for number in range(40):
        store_receipt(conn, str(number), load_receipt(conn, str(number)), intern=True)
    after = conn.execute("SELECT sum(pgsize) FROM dbstat WHERE name='state_meta'").fetchone()[0]
    record_property('state_meta_dbstat_before_bytes', before)
    record_property('state_meta_dbstat_after_bytes', after)
    assert after < before // 10
    assert len(inventory(conn, BLOB_PREFIX)) == 1
    assert len(inventory(conn, REF_PREFIX)) == 40
    assert load_receipt(conn, '39')['policy']['config_json'] == text


def test_autocommit_read_hydrates_one_snapshot_during_lastref_retirement(tmp_path):
    path = tmp_path / 'config.db'
    with closing(sqlite3.connect(path, isolation_level=None)) as writer, closing(
            sqlite3.connect(path, isolation_level=None)) as reader:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE state_meta(key TEXT PRIMARY KEY, value TEXT)')
        writer.execute('BEGIN IMMEDIATE')
        store_receipt(writer, 'one', receipt(), intern=True)
        writer.commit()

        class ReadThenReplace:
            def execute(self, sql, parameters):
                cursor = reader.execute(sql, parameters)
                class Cursor:
                    def fetchone(self):
                        row = cursor.fetchone()
                        cursor.close()
                        writer.execute('BEGIN IMMEDIATE')
                        store_receipt(writer, 'one', receipt('new'))
                        writer.commit()
                        return row
                return Cursor()

        assert load_receipt(ReadThenReplace(), 'one') == receipt()
        assert load_receipt(reader, 'one') == receipt('new')
        assert not reader.in_transaction
        assert inventory(writer, BLOB_PREFIX) == []


def test_real_sessiondb_local_receipt_hydrates(tmp_path):
    from hermes_state import SessionDB
    from hermes_state_local import local_receipt
    with closing(SessionDB(db_path=tmp_path / 'state.db')) as db:
        db._execute_write(lambda conn: store_receipt(conn, 'one', receipt(), intern=True))
        assert local_receipt(db, 'one') == receipt()


def test_reference_lookup_uses_key_index(conn):
    store_receipt(conn, 'one', receipt(), intern=True)
    plan = conn.execute('EXPLAIN QUERY PLAN SELECT 1 FROM state_meta WHERE key GLOB ? LIMIT 1',
                        (REF_PREFIX + 'a' * 64 + ':*',)).fetchall()
    assert any('SEARCH' in row[3] and 'INDEX' in row[3] for row in plan)


@pytest.mark.parametrize('operation', ['decode', 'reconcile'])
def test_snapshot_operations_require_owner_transaction(conn, operation):
    from hermes_state_policy_config import decode_receipt, reconcile_recovered_references
    conn.commit()
    operations = {'decode': lambda: decode_receipt(conn, json.dumps(receipt())),
                  'reconcile': lambda: reconcile_recovered_references(conn)}
    with pytest.raises(RuntimeStoreError, match='runtime_coordination_required'):
        operations[operation]()


def test_missing_receipt_fails_at_storage_boundary(conn):
    with pytest.raises(RuntimeStoreError, match='storage_unavailable'):
        load_receipt(conn, 'missing')


@pytest.mark.parametrize('digest', ['bad', 'A' * 64, {}, 1])
def test_invalid_stored_reference_cannot_be_retired(conn, digest):
    conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                 ('gateway.local_policy.v1:one', json.dumps({'policy': {'config_ref_v1': digest}})))
    before = conn.execute('SELECT * FROM state_meta').fetchall()
    with pytest.raises(RuntimeStoreError, match='storage_unavailable'):
        delete_receipt(conn, 'one')
    assert conn.execute('SELECT * FROM state_meta').fetchall() == before


def test_storing_raw_reference_instead_of_hydrated_policy_is_refused(conn):
    store_receipt(conn, 'one', receipt(), intern=True)
    raw = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?',
                                  ('gateway.local_policy.v1:one',)).fetchone()[0])
    before = conn.execute('SELECT * FROM state_meta ORDER BY key').fetchall()
    with pytest.raises(RuntimeStoreError, match='storage_unavailable'):
        store_receipt(conn, 'two', raw)
    assert conn.execute('SELECT * FROM state_meta ORDER BY key').fetchall() == before


def test_repeated_same_owner_same_config_keeps_its_reference(conn):
    store_receipt(conn, 'one', receipt(), intern=True)
    store_receipt(conn, 'one', receipt(), intern=True)
    assert load_receipt(conn, 'one') == receipt()
    assert len(inventory(conn, BLOB_PREFIX)) == len(inventory(conn, REF_PREFIX)) == 1


def test_existing_corrupt_blob_cannot_be_reused_by_new_owner(conn):
    store_receipt(conn, 'one', receipt(), intern=True)
    conn.execute('UPDATE state_meta SET value=? WHERE key GLOB ?', ('wrong', BLOB_PREFIX + '*'))
    before = conn.execute('SELECT * FROM state_meta ORDER BY key').fetchall()
    with pytest.raises(RuntimeStoreError, match='storage_unavailable'):
        store_receipt(conn, 'two', receipt(), intern=True)
    assert conn.execute('SELECT * FROM state_meta ORDER BY key').fetchall() == before


def test_legacy_only_recovery_does_not_reinterpret_old_malformed_receipts(conn):
    from hermes_state_policy_config import reconcile_recovered_references
    conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                 ('gateway.local_policy.v1:old', 'legacy malformed receipt'))
    before = conn.execute('SELECT * FROM state_meta').fetchall()
    assert reconcile_recovered_references(conn) == 0
    assert conn.execute('SELECT * FROM state_meta').fetchall() == before


def test_non_object_receipt_is_typed_storage_failure(conn):
    from hermes_state_policy_config import decode_receipt
    with pytest.raises(RuntimeStoreError, match='storage_unavailable'):
        decode_receipt(conn, '[]')


@pytest.mark.parametrize('text', [None, 1, {}])
def test_config_payload_must_be_exact_string(conn, text):
    malformed = receipt()
    malformed['policy']['config_json'] = text
    with pytest.raises(RuntimeStoreError, match='storage_unavailable'):
        store_receipt(conn, 'one', malformed, intern=True)
    assert conn.execute('SELECT * FROM state_meta').fetchall() == []


def test_conflicting_inline_and_reference_custody_cannot_be_retired(conn):
    store_receipt(conn, 'one', receipt(), intern=True)
    key = 'gateway.local_policy.v1:one'
    raw = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()[0])
    raw['policy']['config_json'] = '{}'
    conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(raw), key))
    before = conn.execute('SELECT * FROM state_meta ORDER BY key').fetchall()
    with pytest.raises(RuntimeStoreError, match='storage_unavailable'):
        delete_receipt(conn, 'one')
    assert conn.execute('SELECT * FROM state_meta ORDER BY key').fetchall() == before


def test_transaction_decode_missing_blob_is_typed_storage_failure(conn):
    from hermes_state_policy_config import decode_receipt
    store_receipt(conn, 'one', receipt(), intern=True)
    raw = conn.execute('SELECT value FROM state_meta WHERE key=?', ('gateway.local_policy.v1:one',)).fetchone()[0]
    conn.execute('DELETE FROM state_meta WHERE key GLOB ?', (BLOB_PREFIX + '*',))
    with pytest.raises(RuntimeStoreError, match='storage_unavailable'):
        decode_receipt(conn, raw)
