"""Task inventory must not queue behind a concurrent origin-ledger writer."""
import sqlite3
import time

from hermes_cli import kanban_origin_ledger as ledger
from tests.hermes_cli.test_kanban_origin_ledger import world as world, publish, accept  # noqa: PLC0414


def test_projection_preserves_unknown_without_waiting_for_writer(world):
    path, replica, binding = world
    publish(replica, binding)
    accept(world)
    with sqlite3.connect(path) as writer:
        writer.execute('BEGIN IMMEDIATE')
        start = time.monotonic()
        rows = ledger.project(path, replica=replica, gateway='origin', profile='default')
        elapsed = time.monotonic() - start
        assert len(rows) == 1
        assert rows[0]['unfinished'] and rows[0]['stale']
        assert rows[0]['status'] == 'unknown'
        assert elapsed < 2, f'projection queued behind a writer for {elapsed:.3f}s'
    # The competing writer has gone; normal refresh remains usable.
    ledger.refresh_observations(path, replica)
    assert ledger.project(path, replica=replica, gateway='origin', profile='default')[0]['unfinished']
