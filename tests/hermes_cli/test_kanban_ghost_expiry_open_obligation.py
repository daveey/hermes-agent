"""A retained remote link whose owner republishes the card without it expires only on verified closure. [daveey]

A reprovisioned Board worker restores its cards from the Board, but not ``kanban_notify_subs`` (the hub collector's
only source of a card's ``sessions``), so it republishes every pre-reprovision card with ``sessions: []``. The
origin's retained snapshot row is then the only copy of its obligation: expiring it as a ghost while the fresh owner
row is open, or closed without verified completion, made the origin's chat-link reconcile release the link (hub,
2026-11-11: six shepherd links of one chat in one batch, three of them on open cards).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_sync_client as ksc
from hermes_cli.kanban_origin_ledger import _json
from tests.hermes_cli import kanban_sync_support as support

ME, OWNER = 'macbook', 'worker-1'
LINK = {'gw': ME, 'profile': 'default', 'session': 's_human', 'rel': 'shepherd'}
KEY = 'default/default/t_1'


def observed(task, *, verified):
    payload = {k: v for k, v in task.items() if k != 'origin_observation'}
    evidence = {'schema': 1, 'task_hash': hashlib.sha256(_json(payload).encode()).hexdigest(), 'verified': verified}
    return {**payload, 'origin_observation': evidence}


def record(status, *, sessions, verified=False):
    task = {'id': 't_1', 'board': 'default', 'gateway': OWNER, 'profile': 'default', 'status': status,
            'created_at': 1, 'created_by': 'default', 'sessions': sessions, 'origin_bindings': [], 'title': 't_1'}
    return observed(task, verified=verified)


class Hub:
    """hermes-hub's replica: a fresh pull, the owner's fresh scope heartbeat, optionally the feed's ``seq``."""

    def __init__(self, path, *, seq):
        self.path, self.seq, self.n = path, seq, 100
        now = time.time()
        with sqlite3.connect(path) as db:
            extra = 'seq INTEGER,' if seq else ''
            db.executescript('CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);'
                             f'CREATE TABLE replica(gw TEXT, coll TEXT, key TEXT, version INTEGER, {extra} data TEXT,'
                             ' updated_at INTEGER, PRIMARY KEY (gw, coll, key));')
            db.executemany('INSERT INTO meta VALUES (?,?)', [('gateway', ME), ('last_ok', str(now))])
            info = {'gateway': OWNER, 'task_scopes': [
                {'profile': p, 'board': 'default', 'state': 'ok', 'observed_at': now} for p in ('default', 'work')]}
            self._put(db, 'gateway', 'info', info)

    def _put(self, db, coll, key, data):
        self.n += 1
        cols, vals = ('gw, coll, key, version, seq, data, updated_at', (OWNER, coll, key, 1, self.n)) if self.seq \
            else ('gw, coll, key, version, data, updated_at', (OWNER, coll, key, 1))
        marks = ','.join('?' * (len(vals) + 2))
        upsert = ' ON CONFLICT(gw, coll, key) DO UPDATE SET version=version+1, data=excluded.data'
        if self.seq:
            upsert += ', seq=excluded.seq'
        db.execute(f'INSERT INTO replica ({cols}) VALUES ({marks}){upsert}',
                   (*vals, json.dumps(data), int(time.time() * 1000)))

    def publish(self, data):
        with sqlite3.connect(self.path) as db:
            self._put(db, 'tasks', KEY, data)
            db.execute("UPDATE meta SET v=? WHERE k='last_ok'", (str(time.time()),))


@pytest.fixture(params=[True, False], ids=['incremental', 'full-read'])
def world(request, tmp_path, monkeypatch):
    from hermes_cli import profiles
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    hub = Hub(tmp_path / 'hub.db', seq=request.param)
    snapshot: dict = {'rows': None}

    def remote_snapshot(cards=None):
        if cards is None:
            return snapshot['rows']
        snapshot['rows'] = [dict(c) for c in cards]
        return cards

    monkeypatch.setattr(ksa, '_hub_db_path', lambda: hub.path)
    monkeypatch.setattr(ksa, '_remote_snapshot', remote_snapshot)
    # The origin-ledger-free projection, read afresh on every reconcile (no inventory cache in between).
    monkeypatch.setattr(ksa, '_remote_links', lambda stale_ok=False: ksa._legacy_remote_links())
    conn = kbc.connect(home / 'kanban.db')
    ksc.ensure_identity(conn, 'default', ME)
    ksa._reset_cache_for_tests()
    try:
        yield hub, conn
    finally:
        conn.close()
        ksa._reset_cache_for_tests()


def shepherd(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT task_id, chat_session, kind, released FROM task_chat_links WHERE kind='shepherd'")]


def linked_then_republished(hub, conn, republished):
    hub.publish(record('running', sessions=[LINK]))
    links.reconcile(conn, 'default', ME)
    assert shepherd(conn) == [('t_1', 's_human', 'shepherd', 0)]
    hub.publish(republished)  # the reprovisioned owner lost the subscription, not the card
    links.reconcile(conn, 'default', ME)
    return [c for c in ksa._legacy_remote_links() if c['id'] == 't_1']


@pytest.mark.parametrize('status', ['running', 'ready', 'todo', 'scheduled', 'blocked'])
def test_open_owner_row_without_the_link_keeps_the_obligation(world, status):
    hub, conn = world
    rows = linked_then_republished(hub, conn, record(status, sessions=[]))
    assert shepherd(conn) == [('t_1', 's_human', 'shepherd', 0)]
    assert [(r['session'], r['unfinished'], r['released'], r['stale']) for r in rows] == [
        ('s_human', True, False, True)]


@pytest.mark.parametrize('status', ['done', 'archived'])
def test_unverified_terminal_owner_row_keeps_the_obligation_unknown(world, status):
    hub, conn = world
    rows = linked_then_republished(hub, conn, record(status, sessions=[], verified=False))
    assert shepherd(conn) == [('t_1', 's_human', 'shepherd', 0)]
    assert [(r['session'], r['unfinished'], r['released']) for r in rows] == [('s_human', True, False)]


def test_verified_terminal_owner_row_expires_the_ghost(world):
    hub, conn = world
    rows = linked_then_republished(hub, conn, record('done', sessions=[], verified=True))
    assert rows == []
    assert shepherd(conn) == [('t_1', 's_human', 'shepherd', 1)]


def test_verified_terminal_row_of_another_profile_is_no_release(world):
    """Exact identity: a closed record under another owner profile says nothing about this one."""
    hub, conn = world
    hub.publish(record('running', sessions=[LINK]))
    links.reconcile(conn, 'default', ME)
    plain = {k: v for k, v in record('done', sessions=[]).items() if k != 'origin_observation'}
    other = observed({**plain, 'profile': 'work'}, verified=True)
    hub.publish(record('ready', sessions=[]))
    with sqlite3.connect(hub.path) as db:
        hub._put(db, 'tasks', 'work/default/t_1', other)
    links.reconcile(conn, 'default', ME)
    assert shepherd(conn) == [('t_1', 's_human', 'shepherd', 0)]


def test_obligation_released_once_the_open_owner_row_closes_verified(world):
    hub, conn = world
    linked_then_republished(hub, conn, record('ready', sessions=[]))
    assert shepherd(conn) == [('t_1', 's_human', 'shepherd', 0)]
    hub.publish(record('done', sessions=[], verified=True))
    links.reconcile(conn, 'default', ME)
    assert shepherd(conn) == [('t_1', 's_human', 'shepherd', 1)]
