"""Queued create keys are wait handles, not shell/SQL fragments."""
import argparse
import shlex
import sqlite3
import subprocess

import pytest

from hermes_cli import kanban_wait_check as wait

KEY = "core-wt-review:repo#4557:abc' ; $(false)"


@pytest.fixture
def cards(tmp_path, monkeypatch):
    path = tmp_path / 'cards.db'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE tasks(id TEXT, status TEXT, idempotency_key TEXT, created_at INTEGER)')
    db.commit()
    monkeypatch.setattr(wait, '_db', lambda board: path)
    from hermes_cli import kanban_sync_client
    monkeypatch.setattr(kanban_sync_client, 'shadow_path', lambda slug: tmp_path / 'absent.db')
    yield db
    db.close()


def put(db, status, *, tid='new', created=2, key=KEY):
    db.execute('INSERT INTO tasks VALUES(?,?,?,?)', (tid, status, key, created))
    db.commit()


@pytest.mark.parametrize('status,expected', [(None, 1), ('running', 1), ('done', 0), ('archived', 0)])
def test_key_status(cards, status, expected):
    if status:
        put(cards, status)
    rc, text = wait.check_local('key', KEY)
    assert rc == expected, text
    if status:
        assert text.split()[:2] == ['new', status]


def test_newest_card_wins(cards):
    put(cards, 'done', tid='old', created=1)
    put(cards, 'running')
    assert wait.check_local('key', KEY)[0] == 1


def test_until_and_from_are_task_rules(cards):
    put(cards, 'running')
    assert wait.check_local('key', KEY, until=('done',))[0] == 1
    assert wait.check_local('key', KEY, until=('running',))[0] == 0
    assert wait.check_local('key', KEY, until=('running',), since=('running',))[0] == 1


def test_unreadable_is_not_pending(cards):
    cards.execute('DROP TABLE tasks')
    cards.commit()
    assert wait.check_local('key', KEY)[0] == 2


def test_remote_key_argv_reads_selected_board(cards, monkeypatch, capsys):
    put(cards, 'done')
    calls = []

    def ssh(argv, **kwargs):
        calls.append((argv, kwargs))
        inner = shlex.split(argv[-1].split('; ', 1)[1])
        assert inner[:4] == ['hermes', 'kanban', 'wait-check', 'key']
        assert inner[4] == KEY
        assert inner[inner.index('--board') + 1] == 'reviews'
        rc, text = wait.check_local('key', inner[4], board='reviews', until=('done',))
        return subprocess.CompletedProcess(argv, rc, text, '')

    monkeypatch.setattr(wait.subprocess, 'run', ssh)
    args = argparse.Namespace(check='key', target=KEY, gateway='remote', board='reviews', until='done', since='')
    assert wait.run(args) == 0
    args.since = 'done'
    assert wait.run(args) == 1
    assert all(not kwargs.get('shell') for _, kwargs in calls)
    assert 'unchanged since park' in capsys.readouterr().out


def test_key_parser():
    from hermes_cli.kanban import build_parser
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest='command'))
    args = parser.parse_args(['kanban', 'wait-check', 'key', KEY, '--gateway', 'hub', '--board', 'reviews'])
    assert (args.check, args.target, args.gateway, args.board) == ('key', KEY, 'hub', 'reviews')


@pytest.mark.parametrize('age,expected', [(0, 0), (100000, 2)])
def test_restricted_hub_key_uses_only_fresh_board_shadow(cards, tmp_path, monkeypatch, age, expected):
    import time
    from hermes_cli import kanban_sync_client
    shadow = tmp_path / 'shadow.db'
    db = sqlite3.connect(shadow)
    try:
        db.executescript('CREATE TABLE tasks(id TEXT,status TEXT,idempotency_key TEXT,created_at INTEGER);'
                         'CREATE TABLE kanban_sync_state(pulled_at REAL);')
        db.execute('INSERT INTO kanban_sync_state VALUES(?)', (time.time() - age,))
        put(db, 'done')
    finally:
        db.close()
    monkeypatch.setattr(kanban_sync_client, 'shadow_path', lambda slug: shadow)
    monkeypatch.setattr(wait.subprocess, 'run', lambda *a, **kw:
                        subprocess.CompletedProcess(a, 2, '', 'hub-read: refused: command'))
    assert wait.check_remote('hub', 'key', KEY)[0] == expected
    assert wait.check_remote('hub', 'key', 'missing')[0] == (1 if age == 0 else 2)
    assert wait.check_remote('foreign', 'key', KEY)[0] == 2
    if not age:
        assert wait.check_remote('hub', 'key', KEY, until=('running',))[0] == 1
        assert wait.check_remote('hub', 'key', KEY, since=('done',))[0] == 1


@pytest.mark.parametrize('age', [0, 100000])
@pytest.mark.parametrize('observation', ['done', 'absent', 'newer-running', 'older-done'])
@pytest.mark.parametrize('remote', [False, True])
def test_local_and_successful_ssh_shadow_freshness(cards, tmp_path, monkeypatch, age, observation, remote):
    import time
    from hermes_cli import kanban_sync_client
    shadow = tmp_path / 'shadow.db'
    with sqlite3.connect(shadow) as db:
        db.executescript('CREATE TABLE tasks(id TEXT,status TEXT,idempotency_key TEXT,created_at INTEGER);'
                         'CREATE TABLE kanban_sync_state(pulled_at REAL);')
        db.execute('INSERT INTO kanban_sync_state VALUES(?)', (time.time() - age,))
        if observation != 'absent':
            put(db, 'running' if observation == 'newer-running' else 'done', created=2)
    if observation == 'newer-running':
        put(cards, 'done', tid='live-old', created=1)
    elif observation == 'older-done':
        put(cards, 'running', tid='live-new', created=3)
    monkeypatch.setattr(kanban_sync_client, 'shadow_path', lambda slug: shadow)

    def ssh(argv, **kwargs):
        assert not kwargs.get('shell')
        rc, text = wait.check_local('key', KEY)
        return subprocess.CompletedProcess(argv, rc, text, '')

    monkeypatch.setattr(wait.subprocess, 'run', ssh)
    result = wait.check_remote('remote', 'key', KEY) if remote else wait.check_local('key', KEY)
    expected = 2 if age else (0 if observation == 'done' else 1)
    assert result[0] == expected, result
    if age:
        assert 'stale' in result[1]


def test_newer_shadow_card_beats_older_live_card(cards, tmp_path, monkeypatch):
    from hermes_cli import kanban_sync_client
    put(cards, 'done', tid='old', created=1)
    shadow = tmp_path / 'shadow.db'
    cards.backup(db := sqlite3.connect(shadow))
    try:
        import time
        db.execute('CREATE TABLE kanban_sync_state(pulled_at REAL)')
        db.execute('INSERT INTO kanban_sync_state VALUES(?)', (time.time(),))
        put(db, 'running')
    finally:
        db.close()
    monkeypatch.setattr(kanban_sync_client, 'shadow_path', lambda slug: shadow)
    assert wait.check_local('key', KEY)[0] == 1
