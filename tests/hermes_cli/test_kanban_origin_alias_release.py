"""Real receipt + legacy shepherd joins must release only proven aliases."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_origin_adapter as adapter, kanban_origin_ledger as ledger
from hermes_cli import kanban_session_activity as activity, profiles
from hermes_state import SessionDB
from tests.hermes_cli.test_kanban_origin_creation import board as board  # noqa: PLC0414 -- pytest fixture registration
from tests.hermes_cli.test_kanban_origin_evidence import create_bound
from tests.hermes_cli.test_kanban_origin_ledger import world as world, accept  # noqa: PLC0414 -- pytest fixture registration


@pytest.fixture
def scenario(world, board, monkeypatch):
    path, replica, binding = world
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(adapter, '_gateway', lambda _: 'origin')
    monkeypatch.setattr(activity, '_hub_db_path', lambda: replica)
    monkeypatch.setattr(activity, '_board_db_paths', list)
    monkeypatch.setattr(activity.time, 'time', lambda: 1001)
    with sqlite3.connect(replica) as db:
        db.execute('ALTER TABLE replica ADD COLUMN updated_at INTEGER DEFAULT 1000000')
        db.execute('CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)')
        db.executemany('INSERT INTO meta VALUES (?,?)', [('gateway', 'origin'), ('last_ok', '1000')])
    activity._reset_cache_for_tests()
    yield path, replica, binding
    activity._reset_cache_for_tests()


def publish(conn, replica, binding, sessions, version):
    owner = binding['owner']
    task = {**owner, 'id': owner['task_id'], 'origin_bindings': [binding],
            'status': kb.get_task(conn, owner['task_id']).status,
            'sessions': [{'gw': 'origin', 'profile': 'default', 'rel': 'shepherd', 'session': s}
                         for s in sessions]}
    conn.execute('SAVEPOINT observation')
    task = ledger.observe_owner_task(conn, task, now=1000)
    conn.execute('RELEASE observation')
    with sqlite3.connect(replica) as db:
        db.execute('DELETE FROM replica')
        db.execute('INSERT INTO replica VALUES (?,?,?,?,?,?)',
                   ('owner', 'tasks', binding['task_record_key'], version, json.dumps(task), 1000000))
        db.execute('INSERT INTO replica VALUES (?,?,?,?,?,?)',
                   ('owner', 'gateway', 'info', version, json.dumps({'gateway': 'owner', 'task_scopes': [
                       {'profile': 'default', 'board': 'default', 'state': 'ok', 'observed_at': 1000}]}),
                    1000000))


def prepare(scenario, aliases, *, verified=True):
    path, replica, binding = scenario
    state = SessionDB(db_path=path.parent / 'state.db')
    for i, sid in enumerate(aliases):
        if i:
            state.end_session(aliases[i - 1], 'compression')
        state.create_session(sid, 'cli', parent_session_id=aliases[i - 1] if i else None)
    state.close()
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding)
        publish(conn, replica, binding, aliases, 3)
        ack = accept(scenario, resolve_session=lambda sid: adapter._resolve_session(path.parent / 'state.db', sid))
        assert ack['status'] == 'accepted'
        destination = path.parent / 'kanban' / 'origin-ledger.db'
        destination.parent.mkdir(exist_ok=True)
        path.rename(destination)
        # Cache the real qualified legacy links before the terminal update.
        assert len(activity._legacy_remote_links()) == len(aliases)
        assert len(activity.session_tasks(aliases)) == 1
        assert activity.active_task_counts() == {aliases[-1]: 1}
        if verified:
            assert kb.complete_task(conn, tid, summary='Built and tested')
        else:
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        publish(conn, replica, binding, aliases, 4)
    return destination


@pytest.mark.parametrize('aliases', [['s_old'], ['s_old', 's_new'], ['s_old', 's_middle', 's_new']])
def test_verified_release_removes_all_proven_legacy_aliases(scenario, aliases):
    prepare(scenario, aliases)
    for _ in range(2):
        activity._cache = None
        assert activity.session_tasks(aliases) == []
        stamped = activity.annotate_kanban_activity([{'id': aliases[-1], '_lineage_ids': aliases}])[0]
        assert stamped.get('kanban_active', 0) == 0
        assert activity.active_task_counts() == {}


def _owner_quiet_for_500s(replica, monkeypatch):
    monkeypatch.setattr(activity.time, 'time', lambda: 1501)
    with sqlite3.connect(replica) as db:
        db.execute("UPDATE meta SET v='1500' WHERE k='last_ok'")


def test_alias_merge_keeps_a_verified_release_when_the_owner_goes_quiet(scenario, monkeypatch):
    """[daveey] Fresh transport, owner heartbeat expired: the verified completion stays released for every alias. It
    used to come back ``unknown`` (2026-11-16: stopped workers 2, 5 and 7 un-released every card they had finished)."""
    _, replica, _ = scenario
    aliases = ['s_old', 's_middle', 's_new']
    prepare(scenario, aliases)
    activity._remote_links()
    _owner_quiet_for_500s(replica, monkeypatch)
    activity._cache = None
    assert activity.session_tasks(aliases) == []
    assert activity.active_task_counts() == {}


@pytest.mark.parametrize('fault', ['missing', 'unreadable', 'expired', 'unverified'])
def test_alias_merge_never_releases_unavailable_or_unverified_evidence(scenario, fault, monkeypatch):
    _, replica, _ = scenario
    aliases = ['s_old', 's_middle', 's_new']
    # An expired owner heartbeat withholds release only from an unverified completion (see the test above).
    prepare(scenario, aliases, verified=fault not in ('unverified', 'expired'))
    # Prime the verified ledger before simulating transport loss.
    activity._remote_links()
    if fault == 'missing':
        replica.unlink()
    elif fault == 'unreadable':
        replica.write_bytes(b'not sqlite')
    elif fault == 'expired':
        _owner_quiet_for_500s(replica, monkeypatch)
    activity._cache = None
    cards = activity.session_tasks(aliases)
    assert len(cards) == 1
    assert cards[0]['status'] == 'unknown' and cards[0]['unfinished']
    assert activity.active_task_counts() == {'s_new': 1}


@pytest.mark.parametrize('fault', ['missing', 'unreadable', 'unrelated', 'cycle'])
@pytest.mark.parametrize('verified', [False, True])
def test_unproven_intermediate_alias_needs_verified_owner_release(scenario, fault, verified):
    """An alias the receipt cannot prove is not released by the receipt; only the owner's own verified
    completion of the bound card releases it (the card is closed whoever links it). [daveey]"""
    path, _, _ = scenario
    aliases = ['s_old', 's_middle', 's_new']
    prepare(scenario, aliases, verified=verified)
    state = path.parent / 'state.db'
    if fault == 'missing':
        state.unlink()
    elif fault == 'unreadable':
        state.write_bytes(b'not sqlite')
    else:
        with sqlite3.connect(state) as db:
            if fault == 'unrelated':
                db.execute("UPDATE sessions SET end_reason='normal',parent_session_id=NULL WHERE id='s_middle'")
                db.execute("UPDATE sessions SET parent_session_id='s_old' WHERE id='s_new'")
            else:
                db.execute("UPDATE sessions SET end_reason='compression' WHERE id='s_new'")
                db.execute("UPDATE sessions SET parent_session_id='s_new' WHERE id='s_old'")
    remaining = [r for r in activity._remote_links() if r['session'] == 's_middle']
    assert len(remaining) == 1
    if verified:
        assert remaining[0]['status'] == 'done' and remaining[0]['released'] and not remaining[0]['unfinished']
        assert activity._replica_bucket(remaining[0]) is None
    else:
        assert remaining[0]['status'] == 'unknown' and remaining[0]['unfinished']


@pytest.mark.parametrize('session', ['s_old', 's_new'])
@pytest.mark.parametrize('foreign', ['origin_gateway', 'origin_profile', 'owner_profile', 'historical', 'unrelated'])
def test_same_text_identity_without_proven_origin_is_preserved(scenario, foreign, session, monkeypatch):
    aliases = ['s_old', 's_new']
    prepare(scenario, aliases)
    legacy = activity._legacy_remote_links()[0]
    # A cached association must retain its own provenance, not inherit the
    # current caller's origin merely because session/key text happens to match.
    legacy.update(session=session, origin_gateway='origin', origin_profile='default')
    if foreign in ('origin_gateway', 'origin_profile'):
        legacy[foreign] = 'foreign'
    elif foreign == 'owner_profile':
        legacy['profile'] = legacy['owner_profile'] = 'foreign'
        legacy['key'] = legacy['key'].replace('/default/', '/foreign/', 1)
    elif foreign == 'historical':
        legacy.pop('origin_gateway')
        legacy.pop('origin_profile')
    else:
        legacy['session'] = 's_unrelated'
    monkeypatch.setattr(activity, '_legacy_remote_links', lambda: [legacy])
    assert activity._remote_links() == [legacy]
