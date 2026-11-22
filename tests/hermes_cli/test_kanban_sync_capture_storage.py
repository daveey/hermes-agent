"""Pending-only provenance substrate; does not certify any capture or migration. [daveey]

The synthetic original tuples below model a future audited capture writer. No
production writer is permitted to stamp one until writer admission/drain ships.
"""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import kanban_sync_support as support

COLUMNS = ("capture_protocol", "original_op_id", "original_created_at", "identity_state")


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / "kanban.db")
    yield conn
    conn.close()


def metadata(conn, table, key, value):
    # Baseline runs reach behavioral assertions rather than missing-column errors.
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if not set(COLUMNS) <= present:
        return None
    return tuple(conn.execute(f"SELECT {','.join(COLUMNS)} FROM {table} WHERE {key}=?", (value,)).fetchone())


def original(conn, *, wire=None):
    op_id = "original-operation"
    conn.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at,wire_json) "
                 "VALUES (?,'tasks','t_original','upsert','{}','',123,?)", (op_id, wire))
    present = {row[1] for row in conn.execute("PRAGMA table_info(kanban_sync_outbox)")}
    assert set(COLUMNS) <= present, "pending provenance storage is missing"
    # INSERT a synthetic certified fixture, never retrofit a historical tuple.
    conn.execute("DELETE FROM kanban_sync_outbox WHERE op_id=?", (op_id,))
    conn.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at,wire_json,"
                 "capture_protocol,original_op_id,original_created_at,identity_state) "
                 "VALUES (?,'tasks','t_original','upsert','{}','',123,?,'capture1',?,123,'original')", (op_id, wire, op_id))
    return op_id


def test_arbitrary_and_trigger_inserts_are_unknown_not_retroactively_certified(board):
    tid = kb.create_task(board, title="not yet certified", assignee="worker")
    row = board.execute("SELECT op_id FROM kanban_sync_outbox WHERE row_uid=?", (tid,)).fetchone()
    assert metadata(board, "kanban_sync_outbox", "op_id", row[0]) == (None, None, None, None)
    board.execute("INSERT INTO kanban_sync_parent_envelopes(task_id,wire) VALUES (?,?)", (tid, '{}'))
    assert metadata(board, "kanban_sync_parent_envelopes", "task_id", tid) == (None, None, None, None)


@pytest.mark.parametrize("assignment", ["op_id='replacement-operation'", "created_at=124"])
def test_in_place_identity_change_taints_and_preserves_original_tuple(board, assignment):
    op_id = original(board)
    board.execute(f"UPDATE kanban_sync_outbox SET {assignment}")
    current = board.execute("SELECT op_id FROM kanban_sync_outbox").fetchone()[0]
    assert metadata(board, "kanban_sync_outbox", "op_id", current) == ('capture1', op_id, 123, 'ambiguous')
    with pytest.raises(sqlite3.IntegrityError, match="capture provenance is immutable"):
        board.execute("UPDATE kanban_sync_outbox SET identity_state='original'")


@pytest.mark.parametrize("assignment", ["fields='{\"title\":\"changed\"}'", "row_uid='t_other'", "table_name='task_events'",
                                        "op='delete'", "hlc='1-0-a'", "base_version=7", "status_origin='new-origin'", "wire_json=NULL"])
def test_changed_frozen_payload_taints_without_erasing_first_capture(board, assignment):
    op_id = original(board, wire=json.dumps({'client_op_id': 'original-operation'}))
    board.execute(f"UPDATE kanban_sync_outbox SET {assignment} WHERE op_id=?", (op_id,))
    assert metadata(board, "kanban_sync_outbox", "op_id", op_id) == ('capture1', op_id, 123, 'ambiguous')


def test_unexposed_coalescing_freeze_and_retry_bookkeeping_do_not_taint(board):
    op_id = original(board)
    board.execute("UPDATE kanban_sync_outbox SET fields='{\"title\":\"coalesced\"}' WHERE op_id=?", (op_id,))
    board.execute("UPDATE kanban_sync_outbox SET wire_json='{}',hlc='1-0-a' WHERE op_id=?", (op_id,))
    board.execute("UPDATE kanban_sync_outbox SET attempts=1,last_error='offline',state='pending' WHERE op_id=?", (op_id,))
    assert metadata(board, "kanban_sync_outbox", "op_id", op_id) == ('capture1', op_id, 123, 'original')


@pytest.mark.parametrize("assignment", ["capture_protocol=NULL", "original_op_id='other-operation'", "original_created_at=124"])
def test_original_capture_fields_cannot_be_rewritten(board, assignment):
    op_id = original(board)
    with pytest.raises(sqlite3.IntegrityError, match="capture provenance is immutable"):
        board.execute(f"UPDATE kanban_sync_outbox SET {assignment} WHERE op_id=?", (op_id,))
    assert metadata(board, "kanban_sync_outbox", "op_id", op_id) == ('capture1', op_id, 123, 'original')


def test_pending_parent_rewrite_taints_and_metadata_is_collected_with_custody(board):
    board.execute("INSERT INTO kanban_sync_parent_envelopes(task_id,wire) VALUES ('t_parent','{}')")
    assert metadata(board, 'kanban_sync_parent_envelopes', 'task_id', 't_parent') == (None, None, None, None)
    board.execute("UPDATE kanban_sync_parent_envelopes SET wire='{\"changed\":true}'")
    assert metadata(board, 'kanban_sync_parent_envelopes', 'task_id', 't_parent') == (None, None, None, 'ambiguous')
    from hermes_cli.kanban_sync_envelope import retire_parent
    retire_parent(board, 't_parent')
    assert board.execute("SELECT count(*) FROM kanban_sync_parent_envelopes").fetchone()[0] == 0


def test_historical_tuple_cannot_be_retrofitted_and_reinstall_preserves_unknown(board):
    board.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at) "
                 "VALUES ('historical-operation','tasks','t_history','upsert','{}','',123)")
    assert metadata(board, 'kanban_sync_outbox', 'op_id', 'historical-operation') == (None, None, None, None)
    with pytest.raises(sqlite3.IntegrityError, match="capture provenance is immutable"):
        board.execute("UPDATE kanban_sync_outbox SET capture_protocol='capture1',original_op_id=op_id,"
                      "original_created_at=created_at,identity_state='original'")
    sync.apply_sync_schema_steps(board)
    assert metadata(board, 'kanban_sync_outbox', 'op_id', 'historical-operation') == (None, None, None, None)


def test_real_containment_rekey_keeps_original_id_time_and_sticky_taint(board, monkeypatch):
    from hermes_cli.kanban_containment_outbox import strip_lifecycle
    op_id = original(board)
    board.execute("UPDATE kanban_sync_outbox SET fields='{\"title\":\"keep\",\"status\":\"ready\"}'")
    monkeypatch.setattr(sync, 'uuid7', lambda: 'same-millisecond-replacement')
    strip_lifecycle(board, 't_original')
    assert metadata(board, 'kanban_sync_outbox', 'op_id', 'same-millisecond-replacement') == ('capture1', op_id, 123, 'ambiguous')
    assert board.execute("SELECT created_at,fields FROM kanban_sync_outbox").fetchone()[:] == (123, '{"title":"keep"}')


def test_taint_rolls_back_with_callers_write_transaction(board):
    op_id = original(board)
    with pytest.raises(RuntimeError, match='rollback'):
        with kbc.write_txn(board):
            board.execute("UPDATE kanban_sync_outbox SET op_id='rolled-back-operation'")
            assert metadata(board, 'kanban_sync_outbox', 'op_id', 'rolled-back-operation')[-1] == 'ambiguous'
            raise RuntimeError('rollback')
    assert metadata(board, 'kanban_sync_outbox', 'op_id', op_id) == ('capture1', op_id, 123, 'original')


def test_delete_reinsert_has_unknown_provenance_not_a_reconstructed_original(board):
    op_id = original(board)
    board.execute('DELETE FROM kanban_sync_outbox WHERE op_id=?', (op_id,))
    board.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at) "
                 "VALUES (?,'tasks','t_original','upsert','{}','',123)", (op_id,))
    assert metadata(board, 'kanban_sync_outbox', 'op_id', op_id) == (None, None, None, None)


def test_guards_survive_capture_trigger_reinstallation_and_detachment(board):
    op_id = original(board)
    sync.drop_triggers(board)
    sync.install_triggers(board)
    board.execute("UPDATE kanban_sync_outbox SET created_at=124 WHERE op_id=?", (op_id,))
    assert metadata(board, 'kanban_sync_outbox', 'op_id', op_id) == ('capture1', op_id, 123, 'ambiguous')
    sync.drop_triggers(board)
    with pytest.raises(sqlite3.IntegrityError, match='capture provenance is immutable'):
        board.execute("UPDATE kanban_sync_outbox SET identity_state=NULL")


def test_unknown_identity_state_cannot_be_upgraded_alone(board):
    board.execute("INSERT INTO kanban_sync_parent_envelopes(task_id,wire) VALUES ('t_unknown','{}')")
    with pytest.raises(sqlite3.IntegrityError, match='capture provenance is immutable'):
        board.execute("UPDATE kanban_sync_parent_envelopes SET identity_state='original'")
    assert metadata(board, 'kanban_sync_parent_envelopes', 'task_id', 't_unknown') == (None, None, None, None)


def test_parent_identity_change_taints_and_original_tuple_is_immutable(board):
    board.execute("INSERT INTO kanban_sync_parent_envelopes(task_id,wire,capture_protocol,original_op_id,"
                 "original_created_at,identity_state) VALUES ('t_parent','{}','capture1','parent-operation',123,'original')")
    board.execute("UPDATE kanban_sync_parent_envelopes SET task_id='t_replacement'")
    assert metadata(board, 'kanban_sync_parent_envelopes', 'task_id', 't_replacement') == ('capture1','parent-operation',123,'ambiguous')
    for assignment in ["original_op_id='changed-operation'", "original_created_at=124", "capture_protocol=NULL", "identity_state=NULL"]:
        with pytest.raises(sqlite3.IntegrityError, match='capture provenance is immutable'):
            board.execute(f"UPDATE kanban_sync_parent_envelopes SET {assignment}")


@pytest.mark.parametrize("table,key", [("kanban_sync_outbox", "op_id"), ("kanban_sync_parent_envelopes", "task_id")])
def test_null_identity_change_taints_exactly_the_changed_row(board, table, key):
    if table == 'kanban_sync_outbox':
        original(board, wire='{}')
    else:
        board.execute("INSERT INTO kanban_sync_parent_envelopes(task_id,wire,capture_protocol,original_op_id,"
                      "original_created_at,identity_state) VALUES ('t_parent','{}','capture1','parent-operation',123,'original')")
    rowid = board.execute(f"SELECT rowid FROM {table}").fetchone()[0]
    before = tuple(board.execute(f"SELECT {','.join(COLUMNS)} FROM {table}").fetchone())
    if table == 'kanban_sync_outbox':
        board.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at) "
                      "VALUES (NULL,'tasks','t_unrelated','upsert','{}','',123)")
    else:
        board.execute("INSERT INTO kanban_sync_parent_envelopes(task_id,wire) VALUES (NULL,'{}')")
    other = board.execute(f"SELECT rowid FROM {table} WHERE {key} IS NULL").fetchone()[0]
    board.execute(f"UPDATE {table} SET {key}=NULL WHERE rowid=?", (rowid,))
    actual = tuple(board.execute(f"SELECT {','.join(COLUMNS)} FROM {table} WHERE rowid=?", (rowid,)).fetchone())
    assert actual == (*before[:3], 'ambiguous')
    assert tuple(board.execute(f"SELECT {','.join(COLUMNS)} FROM {table} WHERE rowid=?", (other,)).fetchone()) == (None, None, None, None)
    with pytest.raises(sqlite3.IntegrityError, match='capture provenance is immutable'):
        board.execute(f"UPDATE {table} SET identity_state='original' WHERE rowid=?", (rowid,))


@pytest.mark.parametrize("table", ["kanban_sync_outbox", "kanban_sync_parent_envelopes"])
@pytest.mark.parametrize("assignment", ["capture_protocol='capture1'", "original_op_id='retrofit'", "original_created_at=123"])
def test_unknown_capture_tuple_cannot_be_retrofitted_without_state_change(board, table, assignment):
    if table == 'kanban_sync_outbox':
        board.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at) "
                      "VALUES ('unknown-operation','tasks','t_unknown','upsert','{}','',123)")
    else:
        board.execute("INSERT INTO kanban_sync_parent_envelopes(task_id,wire) VALUES ('t_unknown','{}')")
    with pytest.raises(sqlite3.IntegrityError, match='capture provenance is immutable'):
        board.execute(f"UPDATE {table} SET {assignment}")
    assert tuple(board.execute(f"SELECT {','.join(COLUMNS)} FROM {table}").fetchone()) == (None, None, None, None)


def test_upgrade_preserves_historical_bytes_without_inventing_originality():
    from hermes_cli.kanban_sync_capture_storage import install
    conn = sqlite3.connect(':memory:')
    try:
        conn.executescript('''CREATE TABLE kanban_sync_outbox (
            op_id TEXT PRIMARY KEY, table_name TEXT, row_uid TEXT, op TEXT, fields TEXT,
            base_version INTEGER, hlc TEXT, created_at INTEGER, wire_json TEXT, status_origin TEXT);
            CREATE TABLE kanban_sync_parent_envelopes(task_id TEXT PRIMARY KEY, wire TEXT);
            INSERT INTO kanban_sync_outbox VALUES ('historic-operation','tasks','t_hist','upsert','{}',NULL,'',123,'{}',NULL);
            INSERT INTO kanban_sync_parent_envelopes VALUES ('t_hist','{}');''')
        before = tuple(conn.execute('SELECT * FROM kanban_sync_outbox').fetchone())
        install(conn)
        assert tuple(conn.execute('SELECT * FROM kanban_sync_outbox').fetchone())[:len(before)] == before
        assert metadata(conn, 'kanban_sync_outbox', 'op_id', 'historic-operation') == (None, None, None, None)
        assert metadata(conn, 'kanban_sync_parent_envelopes', 'task_id', 't_hist') == (None, None, None, None)
        install(conn)
        assert metadata(conn, 'kanban_sync_outbox', 'op_id', 'historic-operation') == (None, None, None, None)
    finally:
        conn.close()
