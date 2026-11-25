"""Review fixture: a transfer must not prevent unrelated feed progress."""
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as dbc, kanban_db_sync as sync
from hermes_cli import kanban_home_transfer as transfer
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli import test_kanban_home_source_stop as stop

@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    yield from support.synced_conn(home)

def change(conn, tid, version, seq, **extra):
    row = conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
    fields = {sync._wire_name('tasks', c): row[c] for c in sync.captured_columns(conn, 'tasks')}
    fields.update(id=tid, uid=tid, version=version, deleted_at=None, **extra)
    return sync.RemoteChange(seq=seq, table='tasks', uid=tid, op='upsert', fields=fields,
                             version=version, hlc='2000-0-server', origin_gateway='server')

@pytest.mark.parametrize('stopped', [False, True])
def test_healthy_feed_progress_during_handoff(board, stopped):
    tid, reply = stop.prepared(board)
    other = kb.create_task(board, title='unrelated')
    with dbc.write_txn(board):
        board.execute('UPDATE tasks SET sync_version=3 WHERE id=?', (other,))
        board.execute('DELETE FROM kanban_sync_outbox')
        if stopped:
            transfer.finalize_source(board, reply)
    cursor = board.execute('SELECT cursor FROM kanban_sync_state').fetchone()[0]
    rows = [change(board, other, 4, cursor+1, title='remote edit'),
            change(board, tid, 8, cursor+2, created_on='hub', home_generation=reply['nonce'],
                   home_transfer_pending=reply['nonce'])]
    try:
        sync.apply_remote_changes(board, rows, new_cursor=cursor+2, observers=False)
    except Exception:
        assert board.execute('SELECT title FROM tasks WHERE id=?', (other,)).fetchone()[0] == 'unrelated'
        assert board.execute('SELECT cursor FROM kanban_sync_state').fetchone()[0] == cursor
        raise
    assert board.execute('SELECT title FROM tasks WHERE id=?', (other,)).fetchone()[0] == 'remote edit'
    assert transfer.frozen(board, tid)
