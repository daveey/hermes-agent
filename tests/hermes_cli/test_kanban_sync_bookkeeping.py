"""Bookkeeping writes reflect value changes, not repeated polling calls."""

from hermes_cli import kanban_db_connect as db
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_suspend_ledger as ledger
from tests.hermes_cli import kanban_sync_support as support


def test_repeated_pull_timestamp_and_dispatch_stamp_do_not_write(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = db.connect(home / "kanban.db")
    try:
        from hermes_cli.kanban_sync_client import ensure_identity
        ensure_identity(conn, "default", "macbook")
        sync.mark_pulled(conn, at=100)
        ledger.record_dispatch_pass(conn, now=100)
        before = conn.total_changes
        sync.mark_pulled(conn, at=100)
        ledger.record_dispatch_pass(conn, now=100)
        assert conn.total_changes == before
        sync.mark_pulled(conn, at=101)
        ledger.record_dispatch_pass(conn, now=101)
        assert sync.sync_state(conn)["pulled_at"] == 101
        assert conn.execute("SELECT at FROM kanban_dispatch_stamp").fetchone()[0] == 101
    finally:
        conn.close()


def test_empty_apply_only_updates_changed_cursor_and_clears_applying(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = db.connect(home / "kanban.db")
    try:
        from hermes_cli.kanban_sync_client import ensure_identity
        ensure_identity(conn, "default", "macbook")
        before = conn.total_changes
        sync.apply_remote_changes(conn, [], new_cursor=0, observers=False)
        assert conn.total_changes == before
        sync.apply_remote_changes(conn, [], new_cursor=7, observers=False)
        assert conn.total_changes == before + 1
        assert sync.sync_state(conn)["cursor"] == 7
        conn.execute("UPDATE kanban_sync_state SET applying = 1")
        conn.commit()
        sync.apply_remote_changes(conn, [], new_cursor=7, observers=False)
        assert sync.sync_state(conn)["applying"] == 0
    finally:
        conn.close()
