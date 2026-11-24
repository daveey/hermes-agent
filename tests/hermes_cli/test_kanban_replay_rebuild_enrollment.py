"""Rebuild hold depends on enrollment: legacy-table damage never holds a never-enrolled board.

Regression for review 6043365687 on the replay sender PR.
"""
import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_recover as recovery
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli import test_kanban_db_recover as damage
from tests.hermes_cli.kanban_sync_fake_board import FakeService

MARKER = 'kanban_replay_recovery_loss1'


def _has_marker(path):
    conn = kbc.connect(path)
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (MARKER,)).fetchone() is not None
    finally:
        conn.close()


@pytest.mark.parametrize('table', ['kanban_sync_outbox', 'task_events'])
def test_never_enrolled_legacy_table_damage_keeps_legacy_push(tmp_path, monkeypatch, table):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    path = home / 'kanban.db'
    conn = kbc.connect(path)
    client.ensure_identity(conn, 'default', 'macbook')
    assert conn.execute('SELECT journal_id FROM kanban_sync_state').fetchone()[0] is None
    for i in range(120):
        kb.create_task(conn, title=f'card {i} ' + 'x' * 300, assignee='worker')
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
    conn.close()
    damage._forget(path)
    assert damage._damage_table(path, table)
    rebuilt = tmp_path / 'rebuilt.db'
    report = recovery.RecoveryResult(status='building', db_path=str(path))
    recovery._build(path, rebuilt, None, report)
    recovery._install(rebuilt, path)
    damage._forget(path)
    assert not _has_marker(path)
    hub = FakeService().hub('macbook')
    live = kbc.connect(path)
    try:
        assert client.push(live, hub, 'default', 'macbook', primary=False, max_batches=1).applied >= 1
    finally:
        live.close()


def test_anchor_without_sender_table_still_counts_as_enrolled(tmp_path, monkeypatch):
    """The journal_id fallback: sender table gone, anchor survives -> rebuilt board is held."""
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    path = home / 'kanban.db'
    conn = kbc.connect(path)
    client.ensure_identity(conn, 'default', 'macbook')
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'kanban_replay_sender1' not in names
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET journal_id='surviving-anchor'")
    conn.close()
    damage._forget(path)
    rebuilt = tmp_path / 'rebuilt.db'
    report = recovery.RecoveryResult(status='building', db_path=str(path))
    recovery._build(path, rebuilt, None, report)
    assert _has_marker(rebuilt)
