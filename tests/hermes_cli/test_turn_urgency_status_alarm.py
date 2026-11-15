"""Virtual-clock host incidents, separate from admission scoring."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import turn_urgency_status_alarm as alarm


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli import kanban_triage_sweep as triage
    from hermes_cli import kanban_db_connect as kbc
    from hermes_constants import get_hermes_home
    get_hermes_home().mkdir(parents=True, exist_ok=True)
    (get_hermes_home() / 'config.yaml').write_text(
        'terminal:\n  backend: local\nkanban:\n  sync:\n    enabled: true\n')
    monkeypatch.setattr(wn, 'registry', lambda **kw: None)
    conn = kbc.connect(tmp_path / 'kanban.db')
    from hermes_cli import kanban_sync_client
    kanban_sync_client.ensure_identity(conn, 'default', 'test-worker')
    monkeypatch.setattr(alarm, '_home', lambda: tmp_path)
    monkeypatch.setattr(alarm, '_enabled', lambda: True)
    monkeypatch.setattr(alarm, '_default_board', lambda conn: True)
    monkeypatch.setattr(sync, 'board_mode', lambda conn: 'primary')
    monkeypatch.setattr(wn, '_own', lambda conn: 'test-worker')
    monkeypatch.setattr(triage, '_supervisor_profile', lambda: 'default')
    yield conn, tmp_path
    conn.close()


def cache(home, now):
    path = home / 'cache/kanban-turn-urgency.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({'version': 1, 'board': 'default', 'observed_at': now,
                               'valid_until': now+120, 'source_revision': 'test', 'owners': []}))


def status(home, first, error='gh timed out', success=None):
    path = home / 'cache/kanban-turn-urgency.status.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({'version': 1, 'board': 'default', 'checked_at': first,
                               'last_success_at': success, 'first_failure_at': first,
                               'last_error': error}))


def cards(conn):
    return conn.execute('SELECT * FROM tasks WHERE created_by = ?', (alarm.DETECTOR,)).fetchall()


def comments(conn):
    return [r[0] for r in conn.execute('SELECT body FROM task_comments WHERE author = ?', (alarm.DETECTOR,))]


@pytest.mark.parametrize('error', ['auth failure', 'guard evidence deadline exhausted', 'gh timed out',
                                  'missing cache', 'expired cache'])
def test_exact_fractional_boundary_and_hourly_repeat(rig, error):
    conn, home = rig
    first = 1800000000.875
    if error == 'expired cache':
        cache(home, first)
    elif error != 'missing cache':
        status(home, first, error)
    assert alarm.sweep(conn, now=first) is None
    assert alarm.sweep(conn, now=first+599.999) is None
    card = alarm.sweep(conn, now=first+600)
    assert card and len(cards(conn)) == 1
    assert error in cards(conn)[0]['body']
    assert 'journalctl --user -u hermes-turn-urgency.service' in cards(conn)[0]['body']
    assert cards(conn)[0]['human_request'] is None
    alarm.sweep(conn, now=first+601)
    assert comments(conn) == []
    alarm.sweep(conn, now=first+4200)
    assert len(comments(conn)) == 1
    alarm.sweep(conn, now=first+4201)
    assert len(comments(conn)) == 1 and len(cards(conn)) == 1


def test_recovery_inside_grace_files_nothing(rig):
    conn, home = rig
    first = 1800000000.125
    status(home, first)
    alarm.sweep(conn, now=first)
    cache(home, first+599)
    status(home, first+599, None, first+599)
    alarm.sweep(conn, now=first+599)
    assert cards(conn) == [] and comments(conn) == []


def test_recovery_one_comment_and_later_failure_reuses_open_card(rig):
    conn, home = rig
    first = 1800000000.125
    status(home, first)
    card = alarm.sweep(conn, now=first+600)
    cache(home, first+650)
    status(home, first+650, None, first+650)
    alarm.sweep(conn, now=first+650)
    alarm.sweep(conn, now=first+651)
    assert len(comments(conn)) == 1 and 'recovered at ' in comments(conn)[0]
    status(home, first+800, 'auth failure', first+650)
    assert alarm.sweep(conn, now=first+1250) == card
    assert len(cards(conn)) == 1


@pytest.mark.parametrize('terminal', ['done', 'archived'])
def test_closed_incident_gets_new_card(rig, terminal):
    conn, home = rig
    first = 1800000000.125
    status(home, first)
    old = alarm.sweep(conn, now=first+600)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET status = ? WHERE id = ?', (terminal, old))
    new = alarm.sweep(conn, now=first+601)
    assert new != old and len(cards(conn)) == 2


def test_missing_cache_first_seen_survives_detector_restart(rig):
    conn, _home = rig
    first = 1800000000.125
    alarm.sweep(conn, now=first)
    # No process-local streak state: a later sweep reads the persisted receipt.
    assert alarm.sweep(conn, now=first+599.99) is None
    assert alarm.sweep(conn, now=first+600)


def test_cache_publish_crash_does_not_roll_back_success(rig):
    conn, home = rig
    status(home, 900.125, 'observation did not complete', 100.125)
    cache(home, 1000.125)
    alarm.sweep(conn, now=1100.125)
    assert alarm.sweep(conn, now=1599.999) is None
    assert alarm.sweep(conn, now=1600.125)


@pytest.mark.parametrize('bad', [{'owners': [{}]}, {'version': 2},
                                {'valid_until': 9999999999}, {'source_revision': ''}])
def test_invalid_publications_cannot_reset_grace(rig, bad):
    conn, home = rig
    first = 1000.125
    for elapsed in (0, 300, 599.999, 600):
        cache(home, first + elapsed)
        path = home / 'cache/kanban-turn-urgency.json'
        doc = json.loads(path.read_text())
        doc.update(bad)
        path.write_text(json.dumps(doc))
        result = alarm.sweep(conn, now=first + elapsed)
        assert bool(result) == (elapsed == 600)
    assert len(cards(conn)) == 1


def test_second_recovery_inside_hour_is_not_lost(rig):
    conn, home = rig
    status(home, 1000.125)
    alarm.sweep(conn, now=1600.125)
    cache(home, 1650.125)
    alarm.sweep(conn, now=1650.125)
    status(home, 1800.125, success=1650.125)
    alarm.sweep(conn, now=2400.125)
    cache(home, 2450.125)
    alarm.sweep(conn, now=2450.125)
    assert len(comments(conn)) == 2
    assert all('recovered at ' in comment for comment in comments(conn))
    assert len(cards(conn)) == 1


def test_missing_initial_event_self_heals(rig, monkeypatch):
    conn, home = rig
    status(home, 1000.125)
    append = kb._append_event
    def crash(db, card, kind, payload=None, **kw):
        if kind == 'turn_urgency_alarm':
            raise RuntimeError('crash after create')
        return append(db, card, kind, payload, **kw)
    with monkeypatch.context() as patcher:
        patcher.setattr(kb, '_append_event', crash)
        with pytest.raises(RuntimeError, match='crash after create'):
            alarm.sweep(conn, now=1600.125)
    assert len(cards(conn)) == 1
    alarm.sweep(conn, now=1601.125)
    alarm.sweep(conn, now=5201.125)
    assert len(cards(conn)) == 1 and len(comments(conn)) == 1


def test_enabled_timer_does_not_monitor_named_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(alarm.sys, 'platform', 'linux')
    account = tmp_path / 'account'
    monkeypatch.setattr(alarm.Path, 'home', lambda: account)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(account / '.config'))
    unit = account / '.config/systemd/user/timers.target.wants/hermes-turn-urgency.timer'
    unit.parent.mkdir(parents=True)
    unit.write_text('[Timer]')
    monkeypatch.setattr(alarm, '_home', lambda: account / '.hermes')
    assert alarm._enabled()
    monkeypatch.setattr(alarm, '_home', lambda: account / '.hermes/profiles/work')
    assert not alarm._enabled()
    monkeypatch.setattr(alarm, '_home', lambda: account / '.hermes')
    unit.unlink()
    assert not alarm._enabled()


@pytest.mark.parametrize('gate', ['timer', 'board', 'mode'])
def test_disabled_foreign_unsynced_do_not_file(rig, monkeypatch, gate):
    conn, home = rig
    status(home, 1000.125)
    if gate == 'timer':
        monkeypatch.setattr(alarm, '_enabled', lambda: False)
    elif gate == 'board':
        monkeypatch.setattr(alarm, '_default_board', lambda conn: False)
    else:
        monkeypatch.setattr('hermes_cli.kanban_db_sync.board_mode', lambda conn: 'off')
    assert alarm.sweep(conn, now=2000) is None
    assert cards(conn) == []
