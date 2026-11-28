"""No notification is delivered: the very next board read must see each committed change."""
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tests.plugins import test_kanban_board_hot_rows as hot
from tests.plugins import test_kanban_board_incremental as helpers

board = helpers.board
long_card = hot.long_card


@pytest.mark.parametrize('mutation', ['insert', 'edit_old', 'delete_old', 'heartbeat', 'replace'])
def test_next_read_without_notification(long_card, monkeypatch, mutation):
    mod, tid = long_card
    captured = {}
    real = mod._diagnostics_from_inputs

    def observe(inputs):
        captured.update({k: [tuple(r.values()) for r in v] for k, v in inputs[1].items()})
        return real(inputs)

    monkeypatch.setattr(mod, '_diagnostics_from_inputs', observe)
    path = kb.kanban_db_path()
    assert str(path).startswith(str(Path.home()))
    if mutation == 'replace':
        copy = path.with_name('replacement.db')
        with closing(sqlite3.connect(path)) as src, closing(sqlite3.connect(copy)) as dst:
            src.backup(dst)
            dst.execute("UPDATE task_events SET payload='replacement' WHERE task_id=?", (tid,))
            dst.commit()
        for suffix in ('-wal', '-shm'):
            Path(f'{path}{suffix}').unlink(missing_ok=True)
        os.replace(copy, path)
    else:
        with closing(sqlite3.connect(path)) as writer:
            if mutation in ('insert', 'heartbeat'):
                writer.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                               (tid, 'heartbeat' if mutation == 'heartbeat' else 'dispatch_skipped', '{}', 9))
            elif mutation == 'edit_old':
                writer.execute("UPDATE task_events SET payload='old edited' WHERE id="
                               '(SELECT MIN(id) FROM task_events WHERE task_id=?)', (tid,))
            else:
                writer.execute('DELETE FROM task_events WHERE id='
                               '(SELECT MIN(id) FROM task_events WHERE task_id=?)', (tid,))
            writer.commit()
    with closing(sqlite3.connect(path)) as expected:
        rows = expected.execute('SELECT * FROM task_events WHERE task_id=? ORDER BY id', (tid,)).fetchall()
    helpers._read(mod)
    assert captured[tid] == rows
