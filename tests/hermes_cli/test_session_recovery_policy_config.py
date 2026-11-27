"""Actual recovery reconstructs reference custody from surviving policy receipts."""
from contextlib import closing
from types import SimpleNamespace
import hashlib
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
            store_receipt(conn, 'legacy', receipt)
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
    from hermes_cli.session_recovery import SessionRecoveryError
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
    with pytest.raises(SessionRecoveryError, match='policy config custody could not be verified') as caught:
        recover_session_database(source, output, allow_partial=True)
    from hermes_state_runtime import RuntimeStoreError
    assert isinstance(caught.value.__cause__, RuntimeStoreError)


@pytest.mark.parametrize('damage', ['missing_blob', 'corrupt_blob', 'malformed_legacy'])
@pytest.mark.parametrize('allow_partial', [False, True])
def test_recover_cli_reports_unreadable_policy_custody(tmp_path, capsys, damage, allow_partial):
    from hermes_cli.sessions_cmd import _cmd_recover
    source, output = tmp_path / 'source.db', tmp_path / 'recovered.db'
    with closing(SessionDB(source)) as db:
        def write(conn):
            store_receipt(conn, 'one', {'policy': {'config_json': '{}'}}, intern=True)
            if damage == 'missing_blob':
                conn.execute('DELETE FROM state_meta WHERE key GLOB ?', (BLOB_PREFIX + '*',))
            elif damage == 'corrupt_blob':
                conn.execute('UPDATE state_meta SET value=? WHERE key GLOB ?', ('wrong', BLOB_PREFIX + '*'))
            else:
                # Even partial salvage cannot prove a malformed receipt does not
                # own one of the recovered blobs. Refuse before rebuilding/GC.
                conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                             ('gateway.local_policy.v1:legacy', 'not json'))
        db._execute_write(write)
    before = hashlib.sha256(source.read_bytes()).digest()
    result = _cmd_recover(SimpleNamespace(source=source, output=output, allow_partial=allow_partial))
    assert result == 1
    text = capsys.readouterr().out
    assert 'Error: session recovery failed: policy config custody could not be verified' in text
    assert 'source database was not replaced or deleted' in text
    assert 'verified at:' not in text
    assert not output.with_name(output.name + '.recovery.json').exists()
    assert hashlib.sha256(source.read_bytes()).digest() == before


def test_recover_cli_preserves_malformed_inline_only_legacy(tmp_path, capsys):
    from hermes_cli.sessions_cmd import _cmd_recover
    source, output = tmp_path / 'source.db', tmp_path / 'recovered.db'
    with closing(SessionDB(source)) as db:
        db._execute_write(lambda conn: conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                                                   ('gateway.local_policy.v1:legacy', 'not json')))
    assert _cmd_recover(SimpleNamespace(source=source, output=output, allow_partial=True)) == 0
    assert 'verified at:' in capsys.readouterr().out
    with closing(sqlite3.connect(output)) as conn:
        assert conn.execute('SELECT value FROM state_meta WHERE key=?',
                            ('gateway.local_policy.v1:legacy',)).fetchone()[0] == 'not json'
