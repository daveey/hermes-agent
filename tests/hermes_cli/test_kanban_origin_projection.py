"""Authoritative receipt projections survive replica loss and isolate owners."""
import sqlite3

from tests.hermes_cli.test_kanban_origin_ledger import world, publish, accept
from hermes_cli import kanban_origin_ledger as ledger


def test_task_list_and_counts_share_durable_projection(world, monkeypatch):
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli import kanban_origin_adapter as adapter
    from hermes_cli import profiles
    import hermes_constants

    path, replica, binding = world
    publish(replica, binding)
    accept(world)
    home = path.parent
    destination = home / 'kanban' / 'origin-ledger.db'
    destination.parent.mkdir()
    path.rename(destination)
    replica.unlink()
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: home)
    monkeypatch.setattr(adapter, '_gateway', lambda _: 'origin')
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(activity, '_hub_db_path', lambda: None)
    monkeypatch.setattr(activity, '_board_db_paths', list)
    monkeypatch.setattr(activity, '_remote_snapshot', lambda *args: None)
    monkeypatch.setattr(activity, '_remote_last', None)
    monkeypatch.setattr(activity, '_cache', None)
    cards = activity.session_tasks(['s_old', 's_current'])
    assert len(cards) == 1
    assert cards[0]['key'] == 'owner/work/build/t_one'
    assert cards[0]['status'] == 'unknown'
    assert activity.active_task_counts() == {'s_current': 1}
    assert activity.blocked_task_counts() == {}


def test_projection_retains_receipt_after_replica_disappears(world):
    path, replica, binding = world
    publish(replica, binding)
    accept(world)
    replica.unlink()
    rows = ledger.project(path, replica=replica, gateway='origin', profile='default')
    assert len(rows) == 1
    row = rows[0]
    assert row['key'] == 'owner/work/build/t_one'
    assert row['profile'] == 'work'
    assert row['session'] == 's_current'
    assert row['origin_session'] == 's_old'
    assert row['unfinished'] and row['stale']
    assert row['status'] == 'unknown'
    assert row['reported_status'] == 'running'
    assert ledger.project(path, replica=replica, gateway='origin', profile='other') == []


def test_projection_unavailable_replica_never_exposes_released_as_done(world):
    path, replica, binding = world
    publish(replica, binding)
    accept(world)
    publish(replica, binding, status='done')
    with sqlite3.connect(replica) as db:
        db.execute('UPDATE replica SET version=4')
    ledger.refresh_observations(path, replica)
    replica.write_bytes(b'not sqlite')
    row = ledger.project(path, replica=replica, gateway='origin', profile='default')[0]
    assert row['unfinished'] and row['stale']
    assert row['status'] == 'unknown'
    assert row['reported_status'] == 'done'


def test_projection_current_release_and_reopen(world):
    path, replica, binding = world
    publish(replica, binding)
    accept(world)
    with sqlite3.connect(replica) as db:
        db.execute('CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)')
        db.execute("INSERT INTO meta VALUES ('last_ok','1000')")
    def rows():
        return ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)
    assert rows()[0]['status'] == 'running'
    publish(replica, binding, status='done')
    with sqlite3.connect(replica) as db:
        db.execute('UPDATE replica SET version=4')
    assert rows()[0]['unfinished'] is False
    assert rows()[0]['status'] == 'done'
    publish(replica, binding, status='running')
    with sqlite3.connect(replica) as db:
        db.execute('UPDATE replica SET version=5')
    assert rows()[0]['unfinished'] is True
    assert rows()[0]['status'] == 'running'
