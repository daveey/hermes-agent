"""Actual recovery reconstructs reference custody from surviving policy receipts."""
from contextlib import closing
import sqlite3
import pytest

from hermes_state import SessionDB
from hermes_state_policy_config import BLOB_PREFIX, REF_PREFIX, delete_receipt, load_receipt, store_receipt
from hermes_cli.session_recovery import recover_session_database


def test_recovery_rebuilds_missing_ref_before_last_owner_gc(tmp_path):
    source, output = tmp_path / 'source.db', tmp_path / 'recovered.db'
    receipt = {'policy': {'config_json': ' {"snow":"雪"} ', 'request_json': '{}'}}
    with closing(SessionDB(source)) as db:
        def write(conn):
            store_receipt(conn, 'one', receipt, intern=True)
            store_receipt(conn, 'two', receipt, intern=True)
            conn.execute('DELETE FROM state_meta WHERE key GLOB ?', (REF_PREFIX + '*:two',))
        db._execute_write(write)
    report = recover_session_database(source, output, allow_partial=True)
    assert report['verified'], report
    with closing(sqlite3.connect(output)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        delete_receipt(conn, 'one')
        assert load_receipt(conn, 'two') == receipt
        assert len(conn.execute('SELECT key FROM state_meta WHERE key GLOB ?', (BLOB_PREFIX + '*',)).fetchall()) == 1
        delete_receipt(conn, 'two')
        assert conn.execute('SELECT key FROM state_meta WHERE key GLOB ? OR key GLOB ?',
                            (BLOB_PREFIX + '*', REF_PREFIX + '*')).fetchall() == []



def test_recovery_collects_blob_whose_owner_was_not_salvaged(tmp_path):
    source, output = tmp_path / 'source.db', tmp_path / 'recovered.db'
    with closing(SessionDB(source)) as db:
        def write(conn):
            store_receipt(conn, 'one', {'policy': {'config_json': '{}'}}, intern=True)
            conn.execute('DELETE FROM state_meta WHERE key=?', ('gateway.local_policy.v1:one',))
        db._execute_write(write)
    report = recover_session_database(source, output, allow_partial=True)
    assert report['verified'], report
    with closing(sqlite3.connect(output)) as conn:
        assert conn.execute('SELECT key FROM state_meta WHERE key GLOB ? OR key GLOB ?',
                            (BLOB_PREFIX + '*', REF_PREFIX + '*')).fetchall() == []


@pytest.mark.parametrize('damage', ['missing_blob', 'corrupt_blob', 'missing_blob_and_refs'])
def test_recovery_refuses_unreadable_policy_config(tmp_path, damage):
    from hermes_state_runtime import RuntimeStoreError
    source, output = tmp_path / 'source.db', tmp_path / 'recovered.db'
    with closing(SessionDB(source)) as db:
        def write(conn):
            store_receipt(conn, 'one', {'policy': {'config_json': '{}'}}, intern=True)
            if damage == 'corrupt_blob':
                conn.execute('UPDATE state_meta SET value=? WHERE key GLOB ?', ('wrong', BLOB_PREFIX + '*'))
            else:
                conn.execute('DELETE FROM state_meta WHERE key GLOB ?', (BLOB_PREFIX + '*',))
            if damage == 'missing_blob_and_refs':
                conn.execute('DELETE FROM state_meta WHERE key GLOB ?', (REF_PREFIX + '*',))
        db._execute_write(write)
    with pytest.raises(RuntimeStoreError, match='storage_unavailable'):
        recover_session_database(source, output, allow_partial=True)
