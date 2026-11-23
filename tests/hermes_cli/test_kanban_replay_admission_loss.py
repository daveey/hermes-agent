"""Lost local anchors and rebuilt images must not downgrade to legacy transport."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_recover as recovery
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as sender
from tests.hermes_cli import test_kanban_db_recover as damage
from tests.hermes_cli import test_kanban_recovery_protocol as protocol
from hermes_cli import kanban_db_sync as sync

live = sender.live


def assert_held(conn):
    peer = sender.Receiver()
    with pytest.raises(ValueError, match='restore reconciliation required'):
        client.push(conn, peer, 'default', 'macbook', primary=False, max_batches=1)
    assert peer.calls == [] and peer.sent == []
    assert conn.execute('SELECT replay_restore_required FROM kanban_sync_state').fetchone()[0] == 1


@pytest.mark.parametrize('anchor', [None, 'recreated-journal'])
def test_missing_or_recreated_anchor_is_durably_held_before_transport(live, anchor):
    kb.create_task(live, title='custody stays', assignee='worker')
    before = [tuple(row) for row in live.execute('SELECT * FROM kanban_sync_outbox')]
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_sync_state SET journal_id=?', (anchor,))
    assert_held(live)
    assert [tuple(row) for row in live.execute('SELECT * FROM kanban_sync_outbox')] == before
    assert_held(live)


def test_damaged_database_build_reopen_sets_hold_even_with_intact_anchor(live, tmp_path):
    path = tmp_path / 'damaged.db'
    target = sqlite3.connect(path)
    live.backup(target)
    target.close()
    damage._make_board(path)
    lost = damage._damage_table(path, 'task_events')
    assert lost
    rebuilt = tmp_path / 'rebuilt.db'
    report = recovery.RecoveryResult(status='building', db_path=str(path))
    recovery._build(path, rebuilt, None, report)
    assert report.tables['task_events'].known_lost == len(lost)
    conn = kbc.connect(rebuilt)
    try:
        assert conn.execute('SELECT replay_restore_required FROM kanban_sync_state').fetchone()[0] == 1
        assert_held(conn)
    finally:
        conn.close()


@pytest.mark.parametrize('table,mutable', protocol.CASES)
def test_built_custody_loss_is_reported_not_absence(tmp_path, table, mutable):
    path, rebuilt = tmp_path / 'current.db', tmp_path / 'rebuilt.db'
    with sqlite3.connect(path) as conn:
        protocol.install(conn)
        for name, column, sql in sync._SYNC_COLUMN_DDL:
            if name == 'kanban_sync_state':
                conn.execute(f'ALTER TABLE {name} ADD COLUMN {sql}')
        conn.execute("INSERT INTO kanban_sync_state(board_id,gateway_id,hlc_last,mode) "
                     "VALUES ('default','macbook','','shadow')")
        columns = list(conn.execute(f'PRAGMA table_info({table})'))
        for i in range(100):
            values = [i if 'INT' in col[2].upper() else f'{col[1]}-{i:03}-' + 'x' * 80 for col in columns]
            conn.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in values)})", values)
    lost = damage._damage_table(path, table)
    report = recovery.RecoveryResult(status='building', db_path=str(path))
    recovery._build(path, rebuilt, path, report)
    conn = kbc.connect(rebuilt)
    try:
        losses = json.loads(conn.execute('SELECT loss FROM kanban_replay_recovery_loss1').fetchone()[0])
        assert table in losses[-1]['tables'], 'lost custody treated as absent'
        assert losses[-1]['tables'][table]['known_lost'] == len(lost)
        assert losses[-1]['tables'][table]['filled_from_backup'] == 0
        assert_held(conn)
    finally:
        conn.close()


def test_no_sync_state_at_rebuild_does_not_erase_loss_on_later_install(tmp_path):
    path, rebuilt = tmp_path / 'plain.db', tmp_path / 'rebuilt.db'
    conn = kbc.connect(path)
    conn.close()
    report = recovery.RecoveryResult(status='building', db_path=str(path))
    recovery._build(path, rebuilt, None, report)
    with sqlite3.connect(rebuilt) as conn:
        conn.executescript(sync.SYNC_SCHEMA_SQL)
        for table, column, sql in sync._SYNC_COLUMN_DDL:
            if table == 'kanban_sync_state':
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {sql}')
        conn.execute("INSERT INTO kanban_sync_state(board_id,gateway_id,hlc_last,mode) "
                     "VALUES ('default','macbook','','shadow')")
        conn.commit()
        assert_held(conn)


def test_restore_hold_isolated_to_its_board_in_real_sync_round(live, monkeypatch):
    from pathlib import Path
    path = live.execute('PRAGMA database_list').fetchone()[2]
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_sync_state SET journal_id=NULL')
    peer = sender.Receiver()
    peer.ident = SimpleNamespace(gateway='macbook')
    monkeypatch.setattr(client, 'publish_pickup_if_changed', lambda hub: None)
    from hermes_cli import kanban_source_clones
    monkeypatch.setattr(kanban_source_clones, 'attest_if_due', lambda hub: None)
    original = client.sync_board
    visited = []

    def board_round(slug, db, hub, gateway):
        visited.append(slug)
        if slug == 'healthy':
            return client.RoundResult(board=slug, mode='shadow')
        return original(slug, db, hub, gateway)

    monkeypatch.setattr(client, 'sync_board', board_round)
    results = client.sync_all_once(peer, boards=[('default', Path(path)), ('healthy', Path(path))])
    assert visited == ['default', 'healthy']
    assert 'RestoreRequired' in results[0].error
    assert results[1].error is None
    assert peer.calls == []


@pytest.mark.parametrize('table', ['kanban_sync_state', 'kanban_replay_sender1'])
def test_damaged_singleton_loss_remains_held_after_reopen(live, tmp_path, table):
    path, rebuilt = tmp_path / 'singleton.db', tmp_path / 'rebuilt.db'
    target = sqlite3.connect(path)
    live.backup(target)
    target.close()
    with sqlite3.connect(path) as conn:
        root = conn.execute('SELECT rootpage FROM sqlite_master WHERE name=?', (table,)).fetchone()[0]
        page_size = conn.execute('PRAGMA page_size').fetchone()[0]
    with path.open('r+b') as stream:
        stream.seek((root - 1) * page_size)
        stream.write(b'\xff' * page_size)
    report = recovery.RecoveryResult(status='building', db_path=str(path))
    recovery._build(path, rebuilt, None, report)
    assert report.tables[table].unreadable_paths > 0
    conn = kbc.connect(rebuilt)
    try:
        # A missing sync row may be recreated by ordinary schema installation;
        # the independent rebuild marker must survive that new NULL anchor.
        if conn.execute('SELECT 1 FROM kanban_sync_state').fetchone() is None:
            with kbc.write_txn(conn):
                conn.execute("INSERT INTO kanban_sync_state(board_id,gateway_id,hlc_last,mode) "
                             "VALUES ('default','macbook','','shadow')")
        assert_held(conn)
        with kbc.write_txn(conn):
            conn.execute('UPDATE kanban_sync_state SET replay_restore_required=0')
        assert_held(conn)
    finally:
        conn.close()
