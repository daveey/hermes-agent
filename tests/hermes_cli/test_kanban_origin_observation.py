"""Versioned owner observations must not erase durable origin obligations."""
import sqlite3

from tests.hermes_cli.test_kanban_origin_ledger import world, publish, accept
from hermes_cli import kanban_origin_ledger as ledger


def test_unverified_terminal_observation_retained_and_newer_reopen_restores(world):
    path, replica, binding = world
    publish(replica, binding)
    accept(world)
    publish(replica, binding, status='done')
    with sqlite3.connect(replica) as db:
        db.execute('UPDATE replica SET version=4')
    ledger.refresh_observations(path, replica)
    rows = ledger.obligations(path, gateway='origin', profile='default', sessions=['s_current'])
    assert rows[0]['unfinished'] is True  # a newer done report is not verification
    assert rows[0]['observation_version'] == 4
    publish(replica, binding, status='running')
    with sqlite3.connect(replica) as db:
        db.execute('UPDATE replica SET version=5')
    ledger.refresh_observations(path, replica)
    assert ledger.obligations(path, gateway='origin', profile='default', sessions=['s_current'])[0]['unfinished']


def test_missing_replica_preserves_obligation(world):
    path, replica, binding = world
    publish(replica, binding)
    accept(world)
    replica.unlink()
    ledger.refresh_observations(path, replica)
    assert ledger.obligations(path, gateway='origin', profile='default', sessions=['s_current'])[0]['unfinished']


def test_unproven_or_regressed_completion_cannot_release(world):
    path, replica, binding = world
    publish(replica, binding)
    accept(world)
    for changes, version in [({'status': 'done'}, 2), ({'status': 'done'}, 3),
                             ({'status': 'done', 'origin_bindings': []}, 4),
                             ({'status': 'done', 'profile': 'other'}, 4)]:
        publish(replica, binding, **changes)
        with sqlite3.connect(replica) as db:
            db.execute('UPDATE replica SET version=?', (version,))
        ledger.refresh_observations(path, replica)
        row = ledger.obligations(path, gateway='origin', profile='default', sessions=['s_current'])[0]
        assert row['unfinished']
        assert row['observation_version'] == 3
