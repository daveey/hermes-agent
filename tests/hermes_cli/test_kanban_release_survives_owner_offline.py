"""Origin-bound completions and duplicate card copies when an owner goes quiet. [daveey]

2026-11-16: workers 2, 5 and 7 were stopped and every card they had finished read as unreleased 450 s later (770
links). The replica read was fixed by "A finished remote card stays released when its owner goes silent after
publishing it". Two paths that fix does not reach are pinned here:

- kanban_origin_ledger.project still asked for a live owner scope on every row, so an origin-bound (created)
  link to a verified completion on a quiet owner projected unknown and superseded the released replica card.
- _ledger_links was last-copy-wins when two gateways publish the same card id. Now that a quiet owner's finished
  copy stays released, it must not release a link to a copy that is still open elsewhere.
"""
import json
import sqlite3

import pytest

from hermes_cli import kanban_session_activity as activity
from tests.hermes_cli.test_kanban_origin_creation import board as board  # noqa: PLC0414 -- pytest fixture
from tests.hermes_cli.test_kanban_origin_ledger import world as world  # noqa: PLC0414 -- pytest fixture


@pytest.fixture(autouse=True)
def _origin_profile(monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')


# ---------------------------------------------------------------- one link, two gateways' copies of the card


def _copy(gateway, released):
    return {'id': 't_1', 'board': 'default', 'gateway': gateway, 'key': f'{gateway}/default/default/t_1',
            'session': 's1', 'via': 'shepherd', 'origin_gateway': 'origin', 'origin_profile': 'default',
            'released': released, 'status': 'done' if released else 'running', 'unfinished': not released}


@pytest.mark.parametrize('order', [('hub', 'worker-5'), ('worker-5', 'hub')])
def test_an_open_copy_on_another_gateway_keeps_the_link_open(monkeypatch, order):
    """A stopped worker's finished copy of a card that is running elsewhere must not release the chat's one link."""
    from hermes_cli import kanban_chat_links as links
    from hermes_cli import kanban_db_notify
    rows = [_copy(gw, released=(gw == 'worker-5')) for gw in order]
    monkeypatch.setattr(activity, '_remote_links', lambda stale_ok=False: rows)
    monkeypatch.setattr(kanban_db_notify, 'machine_sessions', lambda *a, **k: set())
    assert [link['released'] for link in links._ledger_links('default', 'origin').values()] == [0]
    rows[:] = [_copy(gw, released=True) for gw in order]
    assert [link['released'] for link in links._ledger_links('default', 'origin').values()] == [1]


# ---------------------------------------------------------------- origin-bound cards (the origin ledger)


def _bound_done(world, *, verified_completion=True):
    """An origin-bound card the owner completed and published, then the owner went quiet 500 s ago."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tests.hermes_cli.test_kanban_origin_evidence import create_bound, publish_observed
    from tests.hermes_cli.test_kanban_origin_ledger import accept
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding)
        publish_observed(conn, replica, binding, version=3)
        accept(world)
        assert kb.complete_task(conn, tid, summary='Built and tested')
        publish_observed(conn, replica, binding, version=4)
    if not verified_completion:
        with sqlite3.connect(replica) as db:
            task = json.loads(db.execute("SELECT data FROM replica WHERE coll='tasks'").fetchone()[0])
            task['origin_observation']['verified'] = False
            db.execute("UPDATE replica SET data=?, version=5 WHERE coll='tasks'", (json.dumps(task),))
    return path, replica


def _project(path, replica, now):
    from hermes_cli import kanban_origin_ledger as ledger
    return ledger.project(path, replica=replica, gateway='origin', profile='default', now=now)[0]


def _hub_synced_at(replica, when):
    with sqlite3.connect(replica) as db:
        db.execute("UPDATE meta SET v=? WHERE k='last_ok'", (str(when),))


def test_origin_bound_verified_completion_survives_an_offline_owner(world, board):
    path, replica = _bound_done(world)
    assert _project(path, replica, 1001)['unfinished'] is False
    _hub_synced_at(replica, 1000 + 37 * 3600)
    row = _project(path, replica, 1001 + 37 * 3600)
    assert (row['status'], row['unfinished'], row['stale']) == ('done', False, False)


def test_origin_bound_unverified_completion_still_needs_a_live_owner(world, board):
    path, replica = _bound_done(world, verified_completion=False)
    _hub_synced_at(replica, 1500)
    row = _project(path, replica, 1501)
    assert row['status'] == 'unknown' and row['unfinished'] and row['stale']


def test_origin_bound_settled_completion_reopens_on_a_newer_owner_record(world, board):
    path, replica = _bound_done(world)
    _hub_synced_at(replica, 1500)
    assert _project(path, replica, 1501)['unfinished'] is False
    with sqlite3.connect(replica) as db:  # the owner comes back with the card open again, heartbeat still old
        task = json.loads(db.execute("SELECT data FROM replica WHERE coll='tasks'").fetchone()[0])
        task = {k: v for k, v in task.items() if k != 'origin_observation'} | {'status': 'running'}
        db.execute("UPDATE replica SET data=?, version=9 WHERE coll='tasks'", (json.dumps(task),))
    assert _project(path, replica, 1501)['unfinished'] is True


def test_origin_bound_settled_completion_needs_a_fresh_hub_sync(world, board):
    path, replica = _bound_done(world)
    row = _project(path, replica, 1001 + 37 * 3600)  # this gateway has not pulled for 37 h
    assert row['status'] == 'unknown' and row['unfinished']


# ---------------------------------------------------------------- the Board removed the owner machine


def _remove_owner(replica, when, path=None):
    """The Board drops the owner gateway: its info row and every task record leave the replica. Another gateway
    keeps publishing, so the replica is not empty. With ``path``, the ledger first observes the owner's last record
    (as every live projection does), so the removal comes after that observation."""
    if path is not None:
        _project(path, replica, 1001)
    with sqlite3.connect(replica) as db:
        db.execute("DELETE FROM replica WHERE gw='owner'")
        db.execute("INSERT OR REPLACE INTO replica VALUES ('other','gateway','info',1,?)",
                   (json.dumps({'gateway': 'other', 'task_scopes': []}),))
        db.execute("UPDATE meta SET v=? WHERE k='last_ok'", (str(when),))


def test_a_removed_owners_verified_completion_stays_released(world, board):
    path, replica = _bound_done(world)
    _remove_owner(replica, 5000, path)
    row = _project(path, replica, 5001)
    assert (row['status'], row['unfinished'], row['stale']) == ('done', False, False)


def test_a_removed_owners_unverified_completion_stays_unknown(world, board):
    path, replica = _bound_done(world, verified_completion=False)
    _remove_owner(replica, 5000, path)
    row = _project(path, replica, 5001)
    assert row['status'] == 'unknown' and row['unfinished']


def test_an_offline_owner_with_its_info_row_is_not_removed(world, board):
    """Only a missing info row AND no record means removed: an owner that is merely offline keeps its info row."""
    path, replica = _bound_done(world, verified_completion=False)
    with sqlite3.connect(replica) as db:
        db.execute("DELETE FROM replica WHERE gw='owner' AND coll='tasks'")
        db.execute("UPDATE meta SET v='5000' WHERE k='last_ok'")
    row = _project(path, replica, 5001)
    assert row['status'] == 'unknown' and row['unfinished']


def test_a_stale_replica_proves_no_removal(world, board):
    path, replica = _bound_done(world)
    _remove_owner(replica, 1000, path)
    row = _project(path, replica, 1000 + 37 * 3600)  # no pull for 37 h
    assert row['status'] == 'unknown' and row['unfinished']


@pytest.mark.parametrize('same_created', [True, False])
def test_a_rehomed_card_drops_the_binding_to_the_removed_owner(world, board, same_created):
    path, replica = _bound_done(world, verified_completion=False)
    _, _, binding = world
    with sqlite3.connect(replica) as db:  # the fixture's record has no created_at: give the card one
        task = json.loads(db.execute("SELECT data FROM replica WHERE coll='tasks' AND gw='owner'").fetchone()[0])
        task['created_at'] = 1234
        db.execute("UPDATE replica SET data=?, version=6 WHERE coll='tasks' AND gw='owner'", (json.dumps(task),))
    _remove_owner(replica, 5000, path)
    moved = {**{k: v for k, v in task.items() if k != 'origin_observation'}, 'gateway': 'other', 'status': 'running'}
    if not same_created:
        moved['created_at'] = (task.get('created_at') or 0) + 1
    key = '/'.join((binding['owner']['profile'], binding['owner']['board'], binding['owner']['task_id']))
    with sqlite3.connect(replica) as db:
        db.execute("INSERT INTO replica VALUES ('other','tasks',?,1,?)", (key, json.dumps(moved)))
    from hermes_cli import kanban_origin_ledger as ledger
    rows = ledger.project(path, replica=replica, gateway='origin', profile='default', now=5001)
    if same_created:
        assert rows == [], 'the new owner publishes the card: the binding to the removed owner is not projected'
    else:
        assert len(rows) == 1 and rows[0]['status'] == 'unknown', 'another card that reuses the id proves nothing'



def test_a_removed_owners_open_card_stays_unknown(world, board):
    """Its last observed status could be out of date by the time the machine was removed: never shown as current."""
    from hermes_cli import kanban_db_connect as kbc
    from tests.hermes_cli.test_kanban_origin_evidence import create_bound, publish_observed
    from tests.hermes_cli.test_kanban_origin_ledger import accept
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        create_bound(conn, binding)
        publish_observed(conn, replica, binding, version=3)
        accept(world)
    _remove_owner(replica, 5000, path)
    row = _project(path, replica, 5001)
    assert (row['status'], row['unfinished'], row['stale']) == ('unknown', True, True)
