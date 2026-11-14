"""Unchanged polls reuse DB scans, not time-sensitive activity annotations."""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.web_routers import profiles
from hermes_state import SessionDB


@pytest.fixture
def listing(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('first', source='cli')
    monkeypatch.setattr(profiles, '_profile_targets', lambda _: [('default', tmp_path)])
    for name in ('kanban', 'goal', 'cron', 'background', 'turn'):
        monkeypatch.setattr(profiles, f'annotate_{name}_activity', lambda rows, **kwargs: None)
    monkeypatch.setattr(profiles, 'annotate_review_wait', lambda rows: None)
    scans = []
    original = profiles._read_profile_db

    def read(*args):
        scans.append(args[0])
        return original(*args)

    monkeypatch.setattr(profiles, '_read_profile_db', read)
    app = FastAPI()
    app.include_router(profiles.sessions_router)
    with TestClient(app) as client:
        yield client, db, scans
    db.close()
    with profiles._PROFILE_SESSION_SCAN_LOCK:
        profiles._PROFILE_SESSION_SCAN_CACHE.clear()


def poll(client, **params):
    response = client.get('/api/profiles/sessions', params=params)
    assert response.status_code == 200
    result = response.json()
    assert result['errors'] == []
    return result


def test_unchanged_poll_reuses_scan_but_next_write_is_visible(listing):
    client, db, scans = listing
    first = poll(client)
    assert [row['id'] for row in first['sessions']] == ['first']
    assert poll(client) == first
    assert len(scans) == 1
    db.create_session('second', source='cli')
    changed = poll(client)
    assert {row['id'] for row in changed['sessions']} == {'first', 'second'}
    assert changed['total'] == 2
    assert len(scans) == 2
    assert poll(client) == changed
    assert len(scans) == 2


def test_query_variants_and_live_annotations(listing, monkeypatch):
    client, db, scans = listing
    db.create_session('cron', source='cron')
    first = poll(client, source='cli', full=True)
    assert [row['id'] for row in first['sessions']] == ['first']
    assert [row['id'] for row in poll(client, source='cron')['sessions']] == ['cron']
    assert poll(client, source='cli', full=True) == first
    assert len(scans) == 2
    # Expiry and activity are live even with no DB mutation and no cache TTL.
    monkeypatch.setattr(profiles, 'time', SimpleNamespace(time=lambda: 9999999999))
    monkeypatch.setattr(profiles, 'annotate_goal_activity',
                        lambda rows: rows[0].update(goal_state='running'))
    again = poll(client, source='cli', full=True)
    assert again['sessions'][0]['is_active'] is False
    assert again['sessions'][0]['goal_state'] == 'running'
    monkeypatch.setattr(profiles, 'annotate_goal_activity', lambda rows: None)
    assert 'goal_state' not in poll(client, source='cli', full=True)['sessions'][0]
    assert len(scans) == 2


def test_effective_config_filter_and_profile_roster_are_live(listing, monkeypatch, tmp_path):
    client, db, scans = listing
    db.create_session('child', source='subagent', parent_session_id='first',
                      model_config={'_delegate_from': 'first'})
    assert [row['id'] for row in poll(client)['sessions']] == ['first']
    from hermes_cli.config import atomic_config_write
    atomic_config_write(tmp_path / 'config.yaml', {'sessions': {'show_subagents': True}})
    assert {row['id'] for row in poll(client)['sessions']} == {'first', 'child'}
    assert len(scans) == 2
    other_home = tmp_path / 'other'
    other_home.mkdir()
    with SessionDB(other_home / 'state.db') as other:
        other.create_session('other', source='cli')
        monkeypatch.setattr(profiles, '_profile_targets',
                            lambda _: [('default', tmp_path), ('other', other_home)])
        assert poll(client)['profile_totals'] == {'default': 2, 'other': 1}
    assert len(scans) == 3


def test_concurrent_unchanged_polls_share_one_scan(listing):
    client, _db, scans = listing
    barrier = threading.Barrier(4)

    def request(_):
        barrier.wait(timeout=10)
        return poll(client)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(request, range(4)))
    assert all(result['total'] == 1 for result in results)
    assert len(scans) == 1


def test_write_during_scan_cannot_publish_stale_generation(listing, monkeypatch):
    client, db, scans = listing
    original = profiles._read_profile_db
    fired = False

    def racing(*args):
        nonlocal fired
        result = original(*args)
        if not fired:
            fired = True
            db.create_session('during-scan', source='cli')
        return result

    monkeypatch.setattr(profiles, '_read_profile_db', racing)
    assert poll(client)['total'] == 1
    assert poll(client)['total'] == 2
    assert len(scans) == 2
    assert poll(client)['total'] == 2
    assert len(scans) == 2
