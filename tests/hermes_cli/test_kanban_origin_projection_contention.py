"""Task inventory must not queue behind a concurrent origin-ledger writer."""
import json
import sqlite3
import time

import pytest

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


@pytest.mark.parametrize('advance', [False, True])
def test_projection_keeps_sync_timestamp_but_not_freshness_during_write_contention(world, advance):
    path, replica, binding = world
    publish(replica, binding)
    accept(world)
    if advance:
        publish(replica, binding, status='blocked')
    with sqlite3.connect(replica) as db:
        if advance:
            db.execute('UPDATE replica SET version=4')
        db.execute('CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)')
        db.execute("INSERT INTO meta VALUES ('last_ok','1000')")
        db.execute("INSERT INTO replica VALUES ('owner','gateway','info',5,?)",
                   (json.dumps({'gateway': 'owner', 'task_scopes': [
                       {'profile': 'work', 'board': 'build', 'state': 'ok', 'observed_at': 1000}]}),))

    with sqlite3.connect(path) as writer:
        writer.execute('BEGIN IMMEDIATE')
        start = time.monotonic()
        row, = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)
        elapsed = time.monotonic() - start
        assert elapsed < 2, f'projection queued behind a writer for {elapsed:.3f}s'
        assert row['synced_at'] == 1000
        assert row['stale'] and row['unfinished']
        assert row['status'] == 'unknown'
        assert row['reported_status'] == 'running'
        assert row['observation_version'] == 3

    # Retry refreshes the durable observation, not the replica's sync timestamp.
    row, = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)
    assert row['synced_at'] == 1000
    assert not row['stale']
    assert row['unfinished']
    assert row['status'] == row['reported_status'] == ('blocked' if advance else 'running')
    assert row['observation_version'] == (4 if advance else 3)
