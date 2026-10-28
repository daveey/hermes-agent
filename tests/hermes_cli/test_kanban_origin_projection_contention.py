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
        assert row['unfinished']
        assert row['reported_status'] == 'running'
        assert row['observation_version'] == 3
        if advance:
            # The busy refresh could not record the newer publication: this row is behind the replica.
            assert row['stale'] and row['status'] == 'unknown'
        else:
            # [daveey] Nothing to advance: the stored observation is exactly the replica's, so the held writer
            # changes nothing. It used to mark every row stale (the sidebar's stale/unknown flicker).
            assert not row['stale'] and row['status'] == 'running'

    # Retry refreshes the durable observation, not the replica's sync timestamp.
    row, = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)
    assert row['synced_at'] == 1000
    assert not row['stale']
    assert row['unfinished']
    assert row['status'] == row['reported_status'] == ('blocked' if advance else 'running')
    assert row['observation_version'] == (4 if advance else 3)


def _fresh_replica(world, last_ok):
    path, replica, binding = world
    publish(replica, binding)
    accept(world)
    with sqlite3.connect(replica) as db:
        db.execute('CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)')
        db.execute("INSERT INTO meta VALUES ('last_ok',?)", (str(last_ok),))
        db.execute("INSERT INTO replica VALUES ('owner','gateway','info',5,?)",
                   (json.dumps({'gateway': 'owner', 'task_scopes': [
                       {'profile': 'work', 'board': 'build', 'state': 'ok', 'observed_at': last_ok}]}),))
    return path, replica


def test_a_projection_with_nothing_to_advance_takes_no_write_lock(world):
    """[daveey] Readers stay readers: with every observation current, refresh never opens a write transaction."""
    path, replica = _fresh_replica(world, 1000)
    ledger.refresh_observations(path, replica)
    with sqlite3.connect(path, timeout=0) as writer:
        writer.execute('BEGIN IMMEDIATE')  # held for the whole call: any write attempt would raise "locked"
        ledger.refresh_observations(path, replica, timeout=0)
        row, = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)
    assert not row['stale'] and row['status'] == 'running'


def test_concurrent_readers_never_see_each_other_as_stale(world):
    """[daveey] Two readers projecting at once (the Desktop backend and a gateway) used to make each other's
    refresh fail with "database is locked", flipping every row stale for that poll."""
    import threading

    path, replica = _fresh_replica(world, 1000)
    stale, errors = [], []
    start = threading.Barrier(2)

    def reader():
        start.wait()
        try:
            for _ in range(150):
                row, = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)
                if row['stale'] or row['status'] != 'running':
                    stale.append(row)
        except Exception as exc:  # surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and stale == []
