"""A re-homed card's owner gateway NAME is reused by a new instance: the old binding must not stay unknown. [daveey]

2026-11-20: worker-a1 was scaled in and its done card re-homed to the hub; a new instance took the name worker-a1.
The name stayed listed, so the removed-owner rule never applied, and the new instance never published the card: the
binding stayed unknown for good and its chat could not declare session_outcome done.
"""
import json
import sqlite3

import pytest

from hermes_cli import kanban_origin_ledger as ledger
from tests.hermes_cli.test_kanban_origin_creation import board as board  # noqa: PLC0414 -- pytest fixture
from tests.hermes_cli.test_kanban_origin_ledger import world as world  # noqa: PLC0414 -- pytest fixture
from tests.hermes_cli.test_kanban_release_survives_owner_offline import (
    _bound_done, _origin_profile as _origin_profile, _project, _rehash,  # noqa: PLC0414 -- autouse fixture
)

NOW = 5001


def _reuse_name(replica, *, beat=5000, other=False, task=None, binding=None):
    """The owner name stays listed with a fresh heartbeat for its scope, but publishes no record of the card."""
    with sqlite3.connect(replica) as db:
        db.execute("DELETE FROM replica WHERE gw='owner' AND coll='tasks'")
        info = json.loads(db.execute("SELECT data FROM replica WHERE gw='owner' AND coll='gateway'").fetchone()[0])
        info['task_scopes'][0]['observed_at'] = beat  # the scope the collector announced, beating again
        db.execute("UPDATE replica SET data=?, version=7 WHERE gw='owner' AND coll='gateway'", (json.dumps(info),))
        db.execute("UPDATE meta SET v=? WHERE k='last_ok'", (str(NOW - 1),))
        if other:
            moved = {k: v for k, v in task.items() if k != 'origin_observation'} | {'gateway': 'hub', 'status': 'archived'}
            key = '/'.join((binding['owner']['profile'], binding['owner']['board'], binding['owner']['task_id']))
            db.execute("INSERT INTO replica VALUES ('hub','tasks',?,1,?)", (key, json.dumps(moved)))


def _setup(world, *, other, verified=True, status_edit=None):
    path, replica = _bound_done(world, verified_completion=verified)
    _, _, binding = world
    with sqlite3.connect(replica) as db:
        task = json.loads(db.execute("SELECT data FROM replica WHERE coll='tasks' AND gw='owner'").fetchone()[0])
        task['created_at'] = 1234
        task = _rehash(task)
        db.execute("UPDATE replica SET data=?, version=6 WHERE coll='tasks' AND gw='owner'", (json.dumps(task),))
    _project(path, replica, 1001)  # the ledger stores the owner's last record
    return path, replica, binding, task


def test_a_rehomed_card_drops_the_binding_when_the_owner_name_is_reused(world, board):
    path, replica, binding, task = _setup(world, other=True)
    _reuse_name(replica, other=True, task=task, binding=binding)
    rows = ledger.project(path, replica=replica, gateway='origin', profile='default', now=NOW)
    assert rows == [], 'another gateway publishes the same card: the binding to the reused name is not projected'


def test_another_card_reusing_the_id_does_not_drop_the_binding(world, board):
    path, replica, binding, task = _setup(world, other=True)
    _reuse_name(replica, other=True, task={**task, 'created_at': 9999}, binding=binding)
    rows = ledger.project(path, replica=replica, gateway='origin', profile='default', now=NOW)
    assert len(rows) == 1 and rows[0]['unfinished'] is False, 'no other publisher of THIS card: stored done is final'


def test_a_done_card_absent_from_a_live_reused_owner_is_finished(world, board):
    path, replica, _, _ = _setup(world, other=False)
    _reuse_name(replica)
    row = _project(path, replica, NOW)
    assert (row['status'], row['unfinished']) == ('done', False)


def test_an_open_stored_observation_stays_unknown(world, board):
    from hermes_cli import kanban_db_connect as kbc
    from tests.hermes_cli.test_kanban_origin_evidence import create_bound, publish_observed
    from tests.hermes_cli.test_kanban_origin_ledger import accept
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        create_bound(conn, binding)
        publish_observed(conn, replica, binding, version=3)
        accept(world)
    _project(path, replica, 1001)
    _reuse_name(replica)
    row = _project(path, replica, NOW)
    assert (row['status'], row['unfinished']) == ('unknown', True)


@pytest.mark.parametrize('fault', ['stale-heartbeat', 'no-scope', 'scope-not-ok', 'bad-info', 'no-info', 'stale-replica'])
def test_an_offline_or_unreadable_owner_scope_stays_unknown(world, board, fault):
    path, replica, _, _ = _setup(world, other=False)
    _reuse_name(replica, beat=1000 if fault == 'stale-heartbeat' else 5000)
    with sqlite3.connect(replica) as db:
        info = json.loads(db.execute("SELECT data FROM replica WHERE gw='owner' AND coll='gateway'").fetchone()[0])
        if fault == 'no-scope':
            info['task_scopes'] = []
        elif fault == 'scope-not-ok':
            info['task_scopes'][0]['state'] = 'error'
        if fault == 'bad-info':
            db.execute("UPDATE replica SET data='{' WHERE gw='owner' AND coll='gateway'")
        elif fault == 'no-info':
            # still listed by another record of the owner, but with no inventory
            db.execute("DELETE FROM replica WHERE gw='owner' AND coll='gateway'")
            db.execute("INSERT INTO replica VALUES ('owner','tasks','work/build/t_unrelated',1,'{}')")
        else:
            db.execute("UPDATE replica SET data=? WHERE gw='owner' AND coll='gateway'", (json.dumps(info),))
        if fault == 'stale-replica':
            db.execute("UPDATE meta SET v='1000' WHERE k='last_ok'")
    row = _project(path, replica, NOW if fault != 'stale-replica' else NOW + 37 * 3600)
    assert row['status'] == 'unknown' and row['unfinished'] and row['stale']


@pytest.mark.parametrize('fault', ['stale-heartbeat', 'no-scope', 'scope-not-ok'])
def test_an_offline_owner_keeps_its_binding_even_when_another_gateway_publishes_the_card(world, board, fault):
    """Like ``_retire_moved``, nothing is concluded from an owner whose scope we cannot read live."""
    path, replica, binding, task = _setup(world, other=True)
    _reuse_name(replica, beat=1000 if fault == 'stale-heartbeat' else 5000, other=True, task=task, binding=binding)
    with sqlite3.connect(replica) as db:
        info = json.loads(db.execute("SELECT data FROM replica WHERE gw='owner' AND coll='gateway'").fetchone()[0])
        if fault == 'no-scope':
            info['task_scopes'] = []
        elif fault == 'scope-not-ok':
            info['task_scopes'][0]['state'] = 'error'
        db.execute("UPDATE replica SET data=? WHERE gw='owner' AND coll='gateway'", (json.dumps(info),))
    row = _project(path, replica, NOW)
    assert row['status'] == 'unknown' and row['unfinished'] and row['stale']


def test_only_the_scope_heartbeat_proves_a_reused_owner_live(world, board):
    """A fresh per-task stamp on the stored record (accepted for a PRESENT record) does not prove the scope was read."""
    path, replica, binding, task = _setup(world, other=True)
    _reuse_name(replica, beat=1000, other=True, task=task, binding=binding)
    with sqlite3.connect(path) as db:
        raw, = db.execute('SELECT task_json FROM origin_obligations').fetchone()
        stored = json.loads(raw)
        stored['origin_observation']['observed_at'] = NOW - 1
        db.execute('UPDATE origin_obligations SET task_json=?', (json.dumps(stored),))
    row = _project(path, replica, NOW)
    assert row['status'] == 'unknown' and row['unfinished'] and row['stale']


@pytest.mark.parametrize('other', [True, False])
def test_this_gateways_own_card_is_never_judged_by_the_reused_owner_rule(world, monkeypatch, other):
    """The owner is this gateway: its local read is the authority, not an absence from its own replica."""
    from hermes_cli import kanban_origin_local
    from tests.hermes_cli import test_kanban_origin_ledger as fixtures
    from tests.hermes_cli.test_kanban_remote_completion_receipt import attest, publication
    binding = world[2]
    binding['owner'].update(gateway='origin', profile='default')
    binding['task_record_key'] = 'default/build/t_one'
    task = {**publication(binding), 'created_at': 1234}
    attest(task)
    with sqlite3.connect(world[1]) as db:
        db.execute("INSERT INTO replica VALUES ('origin','tasks',?,3,?)", (binding['task_record_key'], json.dumps(task)))
    fixtures.accept(world, sender='gw:origin')
    with sqlite3.connect(world[1]) as db:
        db.execute("DELETE FROM replica WHERE gw IS NOT NULL")
        db.execute('CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)')
        db.execute("INSERT OR REPLACE INTO meta VALUES ('last_ok', ?)", (str(NOW - 1),))
        info = {'gateway': 'origin', 'task_scopes': [{'profile': 'default', 'board': 'build', 'state': 'ok',
                                                      'observed_at': NOW - 1}]}
        db.execute("INSERT INTO replica VALUES ('origin','gateway','info',1,?)", (json.dumps(info),))
        if other:
            db.execute("INSERT INTO replica VALUES ('hub','tasks','default/build/t_one',1,?)",
                       (json.dumps({**task, 'gateway': 'hub'}),))
        else:
            db.execute("INSERT INTO replica VALUES ('hub','gateway','info',1,'{}')")
    running = {**task, 'status': 'running'}
    monkeypatch.setattr(kanban_origin_local, 'observe_local_binding', lambda *a, **kw: running if other else None)
    rows = ledger.project(world[0], replica=world[1], gateway='origin', profile='default', now=NOW)
    assert len(rows) == 1 and rows[0]['unfinished'] is True


def test_an_owner_that_still_publishes_the_card_is_unchanged(world, board):
    """Its own record is current evidence; the new rule applies only to a card the owner does not publish."""
    path, replica, _, _ = _setup(world, other=False)
    with sqlite3.connect(replica) as db:
        db.execute("UPDATE meta SET v=? WHERE k='last_ok'", (str(NOW - 1),))
        db.execute("UPDATE replica SET data=json_set(data,'$.status','running'), version=8 "
                   "WHERE gw='owner' AND coll='tasks'")
    row = _project(path, replica, NOW)
    assert row['reported_status'] == 'running' and row['unfinished'] is True

