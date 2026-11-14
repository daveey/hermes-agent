"""Regression for #2915: consumer reuse and error-handle retirement."""
import sqlite3

import pytest

from hermes_cli import kanban_awaited_card_wake as awaited
from hermes_cli import kanban_lockguard as guard
from hermes_cli import kanban_operator_call as operator
from hermes_cli import kanban_session_activity as activity
from tests.hermes_cli import test_kanban_replica_reader as helpers

replica = helpers.replica


@pytest.mark.parametrize('consumer', ['operator', 'awaited'])
def test_consumer_reuses_reader_across_external_commit(replica, monkeypatch, consumer):
    _path, writer = replica
    writer.execute('CREATE TABLE tasks(id TEXT, status TEXT)')
    writer.execute("INSERT INTO tasks VALUES ('awaited', 'done')")
    writer.commit()
    # Isolate park discovery, not the consumer's reader acquisition or result scan.
    monkeypatch.setattr(awaited, '_parks', lambda _: [('waiting', '', {'awaited'})])
    opens = []
    original = guard.connect

    def counted(*args, **kwargs):
        opens.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(guard, 'connect', counted)

    def read_status():
        if consumer == 'operator':
            with operator._replica(writer) as conn:
                return conn.execute("SELECT status FROM tasks WHERE id='awaited'").fetchone()[0]
        return awaited._replica_finished(writer)['waiting'][1]

    assert read_status() == 'done'
    writer.execute("UPDATE tasks SET status='archived'")
    writer.commit()
    assert read_status() == 'archived'
    assert len(opens) == 1, opens


def test_sqlite_body_error_retires_and_closes_reader(replica):
    path, writer = replica
    failed = None
    try:
        with pytest.raises(sqlite3.OperationalError, match='body sentinel'):
            with helpers.borrow(path) as failed:
                failed.execute('BEGIN')
                assert failed.execute('SELECT value FROM sample').fetchone()[0] == 'before'
                raise sqlite3.OperationalError('body sentinel')
        assert str(path) not in activity._RO_CACHE
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            failed.execute('SELECT 1')
        writer.execute("UPDATE sample SET value='after'")
        writer.commit()
        with helpers.borrow(path) as fresh:
            assert fresh is not failed
            assert not fresh.in_transaction
            assert fresh.execute('SELECT value FROM sample').fetchone()[0] == 'after'
    finally:
        # A close-removal mutant leaves an uncached handle; keep even RED runs isolated.
        if failed is not None:
            failed.close()
