"""Owner evidence is derived from real lifecycle writes, not reported status."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_origin_ledger as ledger
from tests.hermes_cli.test_kanban_origin_creation import board as board  # noqa: PLC0414 -- pytest fixture registration
from tests.hermes_cli.test_kanban_origin_ledger import world as world, publish, accept  # noqa: PLC0414 -- pytest fixture registration


def create_bound(conn, binding, **options):
    tid = kb.create_task(conn, title='Build', assignee='default', completion_contract='local-only',
                         origin_intent={'request_id': 'evidence-create-001',
                                        'owner': {'gateway': 'owner', 'profile': 'default', 'board': 'default'},
                                        'origin': binding['origin']}, **options)
    marker = json.loads(conn.execute('SELECT binding_json FROM origin_creation_intents WHERE task_id=?', (tid,)).fetchone()[0])
    binding.clear()
    binding.update(marker, publication_version=3)
    return tid


@pytest.mark.parametrize('change', ['session', 'missing', 'duplicate', 'owner'])
def test_same_status_stale_binding_cannot_receive_fresh_evidence(world, board, change):
    _, _, binding = world
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding)
        task = {'id': tid, 'status': 'ready', **{k: binding['owner'][k] for k in ('gateway', 'profile', 'board')},
                'origin_bindings': [binding]}
        if change == 'session':
            task['origin_bindings'] = [{**binding, 'origin': {**binding['origin'], 'session': 'stale'}}]
        elif change == 'missing':
            task['origin_bindings'] = []
        elif change == 'duplicate':
            task['origin_bindings'] = [binding, binding]
        else:
            task['profile'] = 'foreign'
        conn.execute('BEGIN')
        with pytest.raises(ValueError, match='origin_observation_binding_changed'):
            ledger.observe_owner_task(conn, task, now=1000)


def test_owner_evidence_requires_existing_snapshot(board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Build', assignee='default')
        assert not conn.in_transaction
        with pytest.raises(ValueError, match='origin_observation_snapshot_required'):
            ledger.observe_owner_task(conn, {'id': tid, 'status': 'ready'}, now=1000)


def publish_observed(conn, replica, binding, *, version, now=1000):
    conn.execute('SAVEPOINT observation')
    status = conn.execute('SELECT status FROM tasks WHERE id=?', (binding['owner']['task_id'],)).fetchone()[0]
    publish(replica, binding, status=status)
    with sqlite3.connect(replica) as db:
        task = json.loads(db.execute("SELECT data FROM replica WHERE coll='tasks'").fetchone()[0])
        task = ledger.observe_owner_task(conn, task, now=now)
        conn.execute('RELEASE observation')
        db.execute("UPDATE replica SET version=?,data=? WHERE coll='tasks'", (version, json.dumps(task)))
        db.execute('CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)')
        db.execute("INSERT OR REPLACE INTO meta VALUES ('last_ok',?)", (str(now),))
        db.execute("INSERT INTO replica VALUES ('owner','gateway','info',6,?)",
                   (json.dumps({'gateway': 'owner', 'task_scopes': [
                       {'profile': binding['owner']['profile'], 'board': binding['owner']['board'], 'state': 'ok',
                        'observed_at': now}]}),))


@pytest.mark.parametrize('completed', [False, True])
def test_archive_releases_only_previously_verified_completion(world, board, completed):
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding)
        publish_observed(conn, replica, binding, version=3)
        accept(world)
        if completed:
            assert kb.complete_task(conn, tid, summary='Built and tested')
        assert kb.archive_task(conn, tid)
        publish_observed(conn, replica, binding, version=4)
    row = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)[0]
    assert row['reported_status'] == 'archived'
    # [daveey] An archive never waits: a verified completion releases it as done work, anything else is closed
    # without counting as completed.
    assert row['unfinished'] is False
    assert row['status'] == 'archived'
    assert row.get('closed_uncompleted', False) is not completed


@pytest.mark.parametrize('fault', ['reopened', 'legacy', 'null', 'malformed'])
def test_archive_without_current_completion_proof_is_closed_not_completed(world, board, fault):
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding)
        publish_observed(conn, replica, binding, version=3)
        accept(world)
        assert kb.complete_task(conn, tid, summary='Built and tested')
        if fault == 'reopened':
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.archive_task(conn, tid)
        if fault != 'reopened':
            payload = {'legacy': None, 'null': 'null', 'malformed': '[]'}[fault]
            conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='archived'", (payload, tid))
        publish_observed(conn, replica, binding, version=4)
    row = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)[0]
    assert row['status'] == 'archived' and not row['unfinished'] and row['closed_uncompleted'] is True


def test_current_owner_verified_completion_releases(world, board):
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding)
        publish_observed(conn, replica, binding, version=3)
        accept(world)
        assert kb.complete_task(conn, tid, summary='Built and tested')
        publish_observed(conn, replica, binding, version=4)
    row = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)[0]
    assert row['status'] == 'done'
    assert row['unfinished'] is False
    assert row['stale'] is False



@pytest.mark.parametrize('terminal', [False, True])
def test_fresh_transport_cannot_refresh_an_offline_owner(world, board, terminal):
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding)
        publish_observed(conn, replica, binding, version=3)
        accept(world)
        if terminal:
            assert kb.complete_task(conn, tid, summary='Built and tested')
        publish_observed(conn, replica, binding, version=4)
    assert ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)[0]['stale'] is False
    with sqlite3.connect(replica) as db:
        db.execute("UPDATE meta SET v='1500' WHERE k='last_ok'")
    row = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1501)[0]
    if terminal:
        # [daveey] A verified completion is settled: the owner going quiet does not reopen it (2026-11-16, stopped
        # workers 2, 5 and 7 turned every card they had finished back to unknown). An open card still goes unknown.
        assert row['status'] == 'done' and not row['unfinished'] and not row['stale']
        return
    assert row['status'] == 'unknown'
    assert row['unfinished'] and row['stale']


@pytest.mark.parametrize('migrated_release', [False, True])
@pytest.mark.parametrize('goal_mode', [False, True])
def test_complete_but_unverified_stays_unfinished(world, board, goal_mode, migrated_release):
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding, goal_mode=goal_mode)
        publish_observed(conn, replica, binding, version=3)
        accept(world)
        if goal_mode:
            # Direct core completion bypasses the CLI/tool judge. A completed
            # event alone cannot prove that the goal judge actually ran.
            assert kb.complete_task(conn, tid, summary='Claims completion')
        else:
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        publish_observed(conn, replica, binding, version=4)
    if migrated_release:
        ledger.refresh_observations(path, replica)
        with sqlite3.connect(path) as db:
            db.execute('UPDATE origin_obligations SET released=1')
    row = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)[0]
    assert row['reported_status'] == 'done'
    assert row['status'] == 'unknown'
    assert row['unfinished'] is True
    assert row['stale'] is False
    assert ledger.obligations(path, gateway='origin', profile='default', sessions=['s_current'])[0]['unfinished']


def test_activity_consumers_share_live_verified_release(world, board, monkeypatch):
    import hermes_constants
    from hermes_cli import kanban_session_activity as activity, kanban_origin_adapter as adapter, profiles

    path, replica, binding = world
    destination = path.parent / 'kanban' / 'origin-ledger.db'
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: path.parent)
    monkeypatch.setattr(adapter, '_gateway', lambda _: 'origin')
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(activity, '_hub_db_path', lambda: replica)
    monkeypatch.setattr(activity, '_board_db_paths', list)
    monkeypatch.setattr(activity, '_legacy_remote_links', list)
    monkeypatch.setattr(activity.time, 'time', lambda: 1001)

    def read():
        activity._reset_cache_for_tests()
        cards = activity.session_tasks(['s_old', 's_current'])
        stamped = activity.annotate_kanban_activity([{'id': 's_current', '_lineage_ids': ['s_old']}])[0]
        return cards, stamped

    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding)
        publish_observed(conn, replica, binding, version=3)
        accept(world)
        destination.parent.mkdir(exist_ok=True)
        path.rename(destination)
        cards, stamped = read()
        assert len(cards) == stamped['kanban_active'] == 1
        # A claimed completion without a lifecycle event stays unfinished.
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        publish_observed(conn, replica, binding, version=4)
        cards, stamped = read()
        assert len(cards) == stamped['kanban_active'] == 1
        assert cards[0]['status'] == 'unknown'
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        conn.commit()
        assert kb.complete_task(conn, tid, summary='Built and tested')
        publish_observed(conn, replica, binding, version=5)
        cards, stamped = read()
        assert cards == []
        assert not stamped.get('kanban_active')
        # Origin transport stays healthy after the owner's last observation expires.
        monkeypatch.setattr(activity.time, 'time', lambda: 1501)
        with sqlite3.connect(replica) as db:
            db.execute("UPDATE meta SET v='1500' WHERE k='last_ok'")
        cards, stamped = read()
        # [daveey] The verified completion stays released: a quiet owner does not bring a finished card back as
        # waiting (2026-11-16, 770 links after workers 2, 5 and 7 were stopped). It used to return as ``unknown``.
        assert cards == []
        assert not stamped.get('kanban_active') and not stamped.get('kanban_remote_stale')


def test_missing_run_end_is_unverified_not_a_collection_error(board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Build', assignee='default', completion_contract='local-only')
        assert kb.complete_task(conn, tid, summary='Built and tested')
        conn.execute('UPDATE task_runs SET ended_at=NULL WHERE task_id=?', (tid,))
        task = {'id': tid, 'status': 'done', 'gateway': 'owner', 'profile': 'default', 'board': 'default'}
        conn.execute('BEGIN')
        assert ledger.observe_owner_task(conn, task, now=1000)['origin_observation']['verified'] is False


def test_owner_observation_proves_current_local_completion(board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Build', assignee='default', completion_contract='local-only')
        assert kb.complete_task(conn, tid, summary='Built and tested')
        task = {'id': tid, 'status': 'done', 'gateway': 'owner', 'profile': 'default', 'board': 'default'}
        observe = getattr(ledger, 'observe_owner_task', None)
        assert callable(observe), 'owner observation producer is missing'
        conn.execute('BEGIN')
        observed = observe(conn, task, now=1000)
        assert observed['origin_observation']['verified'] is True, (
            tuple(conn.execute('SELECT status,completed_at,current_run_id,goal_mode,completion_contract FROM tasks WHERE id=?', (tid,)).fetchone()),
            [tuple(r) for r in conn.execute('SELECT id,status,outcome,ended_at FROM task_runs WHERE task_id=?', (tid,))],
            [tuple(r) for r in conn.execute("SELECT id,run_id,created_at FROM task_events WHERE task_id=? AND kind='completed'", (tid,))],
        )
        # An unchanged task republishes nothing: a later observation is the identical record (liveness is
        # the collector's per-scope heartbeat, not a per-task stamp).
        assert observe(conn, task, now=2000) == observed
        assert task.get('origin_observation') is None


def _quiet_owner(replica, last_ok):
    with sqlite3.connect(replica) as db:
        db.execute("UPDATE meta SET v=? WHERE k='last_ok'", (str(last_ok),))


def _archive_published(world, board, *, completed):
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding)
        publish_observed(conn, replica, binding, version=3)
        accept(world)
        if completed:
            assert kb.complete_task(conn, tid, summary='Built and tested')
        assert kb.archive_task(conn, tid)
        publish_observed(conn, replica, binding, version=4)
    return path, replica


@pytest.mark.parametrize('completed', [False, True])
def test_owner_archive_stays_closed_when_the_owner_goes_quiet(world, board, completed):
    """An explicit owner archive (no artifact verification) ends the origin wait, and the owner going quiet
    afterwards (a sleeping laptop) does not turn it back into an unknown obligation: the archive is the owner's
    latest published record, as a verified completion is (2026-11-18, hub chat 20261111_091916_d2d36a)."""
    path, replica = _archive_published(world, board, completed=completed)
    row = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)[0]
    assert row['status'] == 'archived' and not row['unfinished']
    _quiet_owner(replica, 1500)  # owner silent past its heartbeat window, transport itself fresh
    row = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1501)[0]
    assert row['status'] == 'archived' and not row['unfinished'] and not row['stale']
    assert row.get('closed_uncompleted', False) is not completed


@pytest.mark.parametrize('scope_state', [None, 'error'])
def test_owner_evidenced_archive_settles_without_scope_inventory(world, board, scope_state):
    """The exact owner terminal record, not a heartbeat, closes an archive without proving delivery."""
    path, replica = _archive_published(world, board, completed=False)
    with sqlite3.connect(replica) as db:
        if scope_state is None:
            db.execute("DELETE FROM replica WHERE coll='gateway'")
        else:
            db.execute("UPDATE replica SET data=? WHERE coll='gateway'", (json.dumps({
                'gateway': 'owner', 'task_scopes': [
                    {'profile': 'default', 'board': 'default', 'state': scope_state}]}),))
    row = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)[0]
    assert row['status'] == 'archived' and row['closed_uncompleted']
    assert not row['unfinished'] and not row['stale']
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT released FROM origin_obligations').fetchone()[0] == 0
    # Missing scope evidence cannot make a stale transport authoritative.
    row = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1501)[0]
    assert row['status'] == 'unknown' and row['unfinished'] and row['stale']


def test_archive_not_yet_seen_stays_unknown_then_closes_on_catchup(world, board):
    """Replica lag: until the owner's archive record arrives the card is not known closed; after the catch-up it is."""
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding)
        publish_observed(conn, replica, binding, version=3)
        accept(world)
        assert kb.archive_task(conn, tid)
        # the replica still holds the pre-archive record and the owner is silent: nothing authoritative to trust
        _quiet_owner(replica, 1500)
        row = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1501)[0]
        assert row['unfinished'] and row['status'] == 'unknown'
        publish_observed(conn, replica, binding, version=4, now=1500)
    row = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1501)[0]
    assert row['status'] == 'archived' and not row['unfinished']


def test_nonterminal_owner_going_quiet_still_waits(world, board):
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        create_bound(conn, binding)
        publish_observed(conn, replica, binding, version=3)
        accept(world)
        publish_observed(conn, replica, binding, version=4)
    _quiet_owner(replica, 1500)
    row = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1501)[0]
    assert row['unfinished'] and row['status'] == 'unknown'
