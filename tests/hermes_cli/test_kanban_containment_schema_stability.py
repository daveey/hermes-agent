"""Containment feed preparation must not invalidate unchanged replica readers."""
import contextlib
import sqlite3

import pytest

from hermes_cli import kanban_activity_revision as rev
from hermes_cli import kanban_containment_guard as guard
from hermes_cli import kanban_containment_replica as replica
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import test_kanban_sync_steady_links as f

kanban_home = f.kanban_home
ledger = f.ledger
live = f.live
spies = f.spies
scope = f.scope


@pytest.fixture
def shadow(live, scope):
    conn = ksc.open_shadow(f.BOARD, f.ME)
    guard.install(conn)
    conn.execute('PRAGMA wal_autocheckpoint=0')
    try:
        yield conn
    finally:
        conn.close()


def test_prepare_apply_preserves_schema_and_revision(shadow):
    before = rev.read_key(shadow, rev.SHADOW_TABLES)
    assert before is not None
    cookie = shadow.execute('PRAGMA schema_version').fetchone()[0]
    for _ in range(2):
        replica.prepare_apply(shadow, [])
        assert shadow.execute('PRAGMA schema_version').fetchone()[0] == cookie
        assert rev.read_key(shadow, rev.SHADOW_TABLES) == before


def test_settled_install_is_read_only_but_registers_functions(shadow):
    sql = []
    shadow.set_trace_callback(sql.append)
    try:
        guard.install(shadow)
    finally:
        shadow.set_trace_callback(None)
    assert not any(s.upper().startswith(('BEGIN', 'SAVEPOINT', 'INSERT', 'CREATE', 'DROP')) for s in sql), sql
    path = shadow.execute('PRAGMA database_list').fetchone()[2]
    peer = sqlite3.connect(path)
    try:
        guard.install(peer)
        assert peer.execute("SELECT kanban_containment_write('none')").fetchone()[0] == 0
        assert not peer.in_transaction
    finally:
        peer.close()


@pytest.mark.parametrize('damage', ['missing', 'different', 'column', 'observe', 'table'])
def test_install_repairs_changed_schema_and_preserves_fence(shadow, damage):
    expected = dict(shadow.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name GLOB 'containment_*'"))
    if damage == 'column':
        shadow.execute('ALTER TABLE task_events ADD COLUMN fixture_extra TEXT')
    elif damage == 'table':
        shadow.execute('DROP TABLE containment_observations')
    else:
        name = 'containment_observe_update' if damage == 'observe' else 'containment_task_update'
        shadow.execute(f'DROP TRIGGER {name}')
        if damage != 'missing':
            shadow.execute(f'CREATE TRIGGER {name} BEFORE UPDATE ON tasks BEGIN SELECT 1; END')
    cookie = shadow.execute('PRAGMA schema_version').fetchone()[0]
    guard.install(shadow)
    repaired = dict(shadow.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name GLOB 'containment_*'"))
    after = shadow.execute('PRAGMA schema_version').fetchone()[0]
    assert after > cookie
    if damage in ('different', 'column', 'observe'):
        assert after == cookie + 2, 'only the changed trigger should be replaced'
    if damage == 'missing':
        assert after == cookie + 1
    if damage == 'column':
        assert 'fixture_extra' in repaired['containment_event_update']
    else:
        assert repaired == expected
    shadow.execute("INSERT INTO tasks (id,title,status,created_at,containment_episode) VALUES ('t_held','held','triage',1,'episode')")
    assert shadow.execute("SELECT episode FROM containment_observations WHERE task_id='t_held'").fetchone()[0] == 'episode'
    with pytest.raises(sqlite3.IntegrityError, match='containment_pending'):
        shadow.execute("UPDATE tasks SET status='ready' WHERE id='t_held'")
    stable = shadow.execute('PRAGMA schema_version').fetchone()[0]
    guard.install(shadow)
    assert shadow.execute('PRAGMA schema_version').fetchone()[0] == stable


def test_prepare_apply_repairs_missing_observation_before_marker_clear(shadow):
    shadow.execute("INSERT INTO tasks (id,title,status,created_at,containment_episode) VALUES ('t_repair','held','triage',1,'episode')")
    shadow.execute("DELETE FROM containment_observations WHERE task_id='t_repair'")
    # A different episode for the same task must not mask the missing marker.
    shadow.execute("INSERT INTO containment_observations VALUES ('t_repair','older')")
    cookie = shadow.execute('PRAGMA schema_version').fetchone()[0]
    replica.prepare_apply(shadow, [])
    assert shadow.execute("SELECT 1 FROM containment_observations WHERE task_id='t_repair' AND episode='episode'").fetchone()
    assert shadow.execute('PRAGMA schema_version').fetchone()[0] == cookie
    change = sync.RemoteChange(1, 'tasks', 't_repair', 'upsert',
                               {'status': 'ready', 'containment_episode': None}, 100, '', f.OTHER)
    sync.apply_remote_changes(shadow, [change], new_cursor=1, board=f.BOARD, observers=False)
    assert guard.pending(shadow, 't_repair')
    assert shadow.execute("SELECT status FROM tasks WHERE id='t_repair'").fetchone()[0] == 'triage'


def test_rechecks_after_writer_acquisition(shadow, monkeypatch):
    from hermes_cli import kanban_db as kb

    path = shadow.execute('PRAGMA database_list').fetchone()[2]
    peer = sqlite3.connect(path, isolation_level=None)
    guard.register(peer)
    wanted = shadow.execute("SELECT sql FROM sqlite_master WHERE name='containment_task_update'").fetchone()[0]
    shadow.execute('DROP TRIGGER containment_task_update')
    original = kb.write_txn
    repaired = []

    @contextlib.contextmanager
    def raced(conn, **kwargs):
        # A different installer finishes after the first check, before our lock.
        with original(peer):
            peer.execute(wanted)
        repaired.append(True)
        with original(conn, **kwargs):
            yield

    def unexpected_backfill(conn):
        pytest.fail('a peer already repaired the schema; no backfill is needed')

    monkeypatch.setattr(kb, 'write_txn', raced)
    monkeypatch.setattr(guard, '_install_schema', unexpected_backfill)
    try:
        guard.install(shadow)
        assert repaired == [True]
    finally:
        peer.close()


def test_heartbeat_feed_page_keeps_links_inputs_and_skips(shadow, live, ledger, kanban_home, spies):
    settled = f._settled(live, ledger, kanban_home, spies)
    def inputs():
        return ksc._links_inputs(live, kanban_home / 'kanban.db', f.BOARD, f.ME,
                                 sync.board_mode(live), {}, ())
    before = inputs()
    assert before is not None
    for seq in (1, 2):
        change = sync.RemoteChange(seq, 'task_events', sync.uuid7(), 'upsert',
                                   {'task_id': 't_foreign', 'kind': 'heartbeat', 'payload': '{}', 'created_at': seq},
                                   seq, '', f.OTHER)
        sync.apply_remote_changes(shadow, [change], new_cursor=seq, board=f.BOARD, observers=False)
        assert shadow.execute("SELECT COUNT(*) FROM task_events WHERE kind='heartbeat'").fetchone()[0] == seq
        assert inputs() == before
        f._pass(kanban_home)
    assert spies == {'reconcile': 0, 'backfill': 0}
    assert f._links(live) == settled
