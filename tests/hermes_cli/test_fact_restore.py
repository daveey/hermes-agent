"""Backup-API restore invalidates reused revisions and wakes native sources. [daveey]"""
import sqlite3
import threading
from contextlib import closing

import pytest

from hermes_cli.backup_restore import _restore_db_pages
from hermes_cli.fact_capture import CaptureBudget, capture_tasks
from hermes_cli.fact_session_source import capture_sessions
from hermes_cli.fact_source_watch import NativeSourceWatch
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from tests.hermes_cli import test_fact_capture as task_helpers
from tests.hermes_cli import test_fact_session_source as session_helpers

board = task_helpers.board
store = session_helpers.store


def snapshot(path, destination):
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)


def test_task_backup_restore_reused_revision_is_full(board, tmp_path):
    path, writer = board
    key = kb.create_task(writer, title='original', assignee='w')
    saved = tmp_path / 'saved.db'
    snapshot(path, saved)
    with kbc.write_txn(writer):
        writer.execute("UPDATE tasks SET title='later' WHERE id=?", (key,))
    before = capture_tasks(path, None, task_helpers.rows)
    inode = path.stat().st_ino
    assert _restore_db_pages(saved, path)
    # Catch up to the old revision before reading: regression-only tests miss this.
    with kbc.write_txn(writer):
        writer.execute("UPDATE tasks SET title='restored' WHERE id=?", (key,))
    after = capture_tasks(path, before.after, task_helpers.rows)
    assert path.stat().st_ino == inode
    assert after.after.revision == before.after.revision
    assert after.full and after.records[0]['title'] == 'restored'


@pytest.mark.platforms('linux')
def test_session_restore_same_inode_and_revision_native_hint(store, tmp_path):
    db, _, path = store
    db.create_session('a', 'cli')
    saved = tmp_path / 'saved.db'
    snapshot(path, saved)
    db._execute_write(lambda c: c.execute("UPDATE sessions SET input_tokens=20 WHERE id='a'"))
    budget = CaptureBudget(max_seconds=2)
    before = capture_sessions(path, None, session_helpers.rows, budget=budget)
    inode = path.stat().st_ino
    wake = threading.Event()
    watch = NativeSourceWatch((path,), wake.set)
    try:
        assert _restore_db_pages(saved, path)
        assert wake.wait(5)
        watch.check()
        db._execute_write(lambda c: c.execute("UPDATE sessions SET input_tokens=7 WHERE id='a'"))
        after = capture_sessions(path, before.after, session_helpers.rows, budget=budget)
    finally:
        watch.close()
    assert path.stat().st_ino == inode
    assert after.after.content.revision == before.after.content.revision
    assert after.full and after.records[0]['input_tokens'] == 7
