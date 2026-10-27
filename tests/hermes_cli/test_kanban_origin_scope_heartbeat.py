"""Owner liveness comes from the collector's per-scope heartbeat, so unchanged tasks need not republish."""
import json
import sqlite3

import pytest

from tests.hermes_cli.test_kanban_origin_ledger import world as world, accept  # noqa: PLC0414 -- pytest fixture registration
from tests.hermes_cli.test_kanban_origin_creation import board as board  # noqa: PLC0414 -- pytest fixture registration
from tests.hermes_cli.test_kanban_origin_evidence import publish_observed, create_bound
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_origin_ledger as ledger


def project(world, now=1001):
    path, replica, _ = world
    return ledger.project(path, replica=replica, gateway='origin', profile='default', now=now)[0]


def set_scope(replica, binding, **fields):
    with sqlite3.connect(replica) as db:
        info = json.loads(db.execute("SELECT data FROM replica WHERE coll='gateway' AND key='info'").fetchone()[0])
        scope = {'profile': binding['owner']['profile'], 'board': binding['owner']['board'], 'state': 'ok'}
        info['task_scopes'] = [{**scope, **fields}]
        db.execute("UPDATE replica SET data=? WHERE coll='gateway' AND key='info'", (json.dumps(info),))


@pytest.fixture
def published(world, board):
    _, replica, binding = world
    with kbc.connect_closing() as conn:
        create_bound(conn, binding)
        publish_observed(conn, replica, binding, version=3, now=1000)
        accept(world)
    return replica, binding


def test_fresh_scope_heartbeat_keeps_unchanged_task_current(world, published):
    replica, _ = published
    with sqlite3.connect(replica) as db:
        task = json.loads(db.execute("SELECT data FROM replica WHERE coll='tasks'").fetchone()[0])
    assert 'observed_at' not in task['origin_observation']  # the producer no longer stamps tasks
    # 170 s after the task was published, the owner's heartbeat from this sync still proves it current.
    _, binding = published
    set_scope(replica, binding, observed_at=1170)
    row = project(world, now=1170)
    assert row['status'] == 'ready'
    assert not row['stale']


@pytest.mark.parametrize('scope', [
    {'observed_at': 400},                      # heartbeat older than the 450 s window
    {},                                        # no heartbeat and no per-task stamp
    {'observed_at': 1000, 'state': 'unknown'},  # fresh heartbeat on a scope the owner could not read
    {'observed_at': 1000, 'board': 'other'},    # fresh heartbeat for a different board
])
def test_missing_stale_or_unreadable_heartbeat_is_unknown(world, published, scope):
    replica, binding = published
    set_scope(replica, binding, **scope)
    row = project(world)
    assert row['status'] == 'unknown'
    assert row['unfinished'] and row['stale']


def test_legacy_per_task_stamp_without_scope_heartbeat_still_counts(world, published):
    """Records published before scope heartbeats carry the stamp on the task; they stay live meanwhile."""
    replica, binding = published
    set_scope(replica, binding)  # an old collector: no heartbeat on the scope
    with sqlite3.connect(replica) as db:
        task = json.loads(db.execute("SELECT data FROM replica WHERE coll='tasks'").fetchone()[0])
        task['origin_observation']['observed_at'] = 1000  # outside task_hash, as old producers wrote it
        db.execute("UPDATE replica SET data=? WHERE coll='tasks'", (json.dumps(task),))
    path, _, _ = world
    with sqlite3.connect(path) as conn:  # the ledger holds the accepted copy of the published record
        conn.execute("UPDATE origin_obligations SET task_json=?", (json.dumps(task),))
    assert project(world)['status'] == 'ready'
