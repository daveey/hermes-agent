"""Large real SQLite task candidate, default time/key/byte budgets. [daveey]"""
import time

import pytest

from hermes_cli import fact_capture, kanban_db_connect as kbc
from hermes_cli.fact_capture import CaptureBudget, CapturePending
from hermes_cli.fact_task_source import TaskContentSource
from tests.hermes_cli import test_fact_capture as helpers

board = helpers.board


def test_large_task_candidate_pages_bytes_and_in_lists(board, monkeypatch):
    path, writer = board
    monkeypatch.setattr(fact_capture, 'time', time)
    with kbc.write_txn(writer):
        writer.executemany(
            "INSERT INTO tasks(id,title,status,created_at) VALUES (?,?,'todo',1)",
            ((f'large-{i:05}', 'x' * 2048) for i in range(2501)))
    batches = []
    def hydrate(conn, ids):
        batches.append(len(ids))
        marks = ','.join('?' * len(ids))
        return (dict(zip(('id', 'title'), row))
                for row in conn.execute(f'SELECT id,title FROM tasks WHERE id IN ({marks})', ids))
    source = TaskContentSource('tasks', path, hydrate)
    for _ in range(200):
        try:
            result = source.capture(None, CaptureBudget())
            break
        except CapturePending:
            continue
    else:
        pytest.fail('large task bootstrap did not converge')
    assert len(result.records) == 2501
    assert result.payload_bytes > CaptureBudget().max_bytes  # full candidate, never one transaction
    assert max(batches) <= 500
    assert source.capture(None, CaptureBudget()) is result
