"""Native receipt custody must not outlive a task or its primary store."""
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_origin_native as native
from tests.tools import test_kanban_worker_created_chats as fixtures


@pytest.fixture
def store(tmp_path, monkeypatch):
    from hermes_cli import kanban_origin
    home = fixtures.make_home(tmp_path, monkeypatch)
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("here", {"here"}))
    origin = {"gateway": "here", "profile": "default", "session": fixtures.CHAT}
    with kbc.connect_closing() as conn:
        from hermes_cli import kanban_db_sync as sync
        sync.set_mode(conn, "primary")
        task = kb.create_task(conn, title="native root")
        assert native._primary_scope(conn) is not None, (sync._db_key(conn), str(kb.board_own_db_path("default")),
            sync.board_mode(conn), [tuple(r) for r in conn.execute("SELECT board_id,gateway_id,mode FROM kanban_sync_state")])
        native.record(conn, task, {"origin": origin}, 1)
        conn.commit()
        binding = native._receipt(conn, task)
        assert binding is not None
        yield home, conn, task, binding


def test_deleted_receipt_cannot_authorize_or_inherit(store):
    _, conn, task, binding = store
    conn.execute("DELETE FROM tasks WHERE id=?", (task,))
    conn.commit()
    assert native._receipt(conn, task) is None
    assert conn.execute("SELECT COUNT(*) FROM origin_native_receipts WHERE task_id=?", (task,)).fetchone()[0] == 0
    assert native.lookup(binding["origin"], binding["owner"], gateway="here") is None
    child = kb.create_task(conn, title="orphan child")
    native.record(conn, child, {"creator": task}, 2)
    assert native._receipt(conn, child) is None


def test_reused_task_id_does_not_resurrect_receipt(store):
    _, conn, task, _ = store
    # REPLACE's implicit delete does not run delete triggers unless recursive_triggers is on.
    conn.execute("PRAGMA recursive_triggers=OFF")
    conn.execute("INSERT OR REPLACE INTO tasks SELECT * FROM tasks WHERE id=?", (task,))
    assert native._receipt(conn, task) is None


@pytest.mark.parametrize("column,value", [("created_at", 999), ("created_on", "foreign")])
def test_identity_change_revokes_receipt(store, column, value):
    _, conn, task, _ = store
    conn.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, task))
    assert native._receipt(conn, task) is None


def test_update_replace_cannot_transfer_receipt(store):
    _, conn, task, _ = store
    other = kb.create_task(conn, title="untrusted replacement")
    conn.execute("PRAGMA recursive_triggers=OFF")
    conn.execute("UPDATE OR REPLACE tasks SET id=? WHERE id=?", (task, other))
    assert native._receipt(conn, task) is None


def test_normal_task_update_preserves_receipt(store):
    _, conn, task, binding = store
    conn.execute("UPDATE tasks SET title='renamed' WHERE id=?", (task,))
    assert native._receipt(conn, task) == binding


@pytest.mark.parametrize("mode", ["replica", "link", "off"])
def test_mode_change_refuses_reads_and_new_receipts(store, mode):
    _, conn, _, binding = store
    conn.execute("UPDATE kanban_sync_state SET mode=?", (mode,))
    conn.commit()
    assert native.lookup(binding["origin"], binding["owner"], gateway="here") is None
    native.record(conn, "untrusted", {"origin": binding["origin"]}, 2)
    assert native._receipt(conn, "untrusted") is None


def test_shadow_copy_cannot_mint_receipt(store, tmp_path):
    _, conn, task, binding = store
    path = tmp_path / "sync-shadow" / "default.db"
    path.parent.mkdir()
    with sqlite3.connect(path) as shadow:
        conn.backup(shadow)
        shadow.execute("DELETE FROM origin_native_receipts")
        native.record(shadow, task, {"origin": binding["origin"]}, 2)
        assert native._receipt(shadow, task) is None


def test_pinned_shadow_is_not_native_authority(store, tmp_path, monkeypatch):
    _, conn, task, binding = store
    shadow = tmp_path / "shadow.db"
    with sqlite3.connect(shadow) as other:
        conn.backup(other)
    # Only shadow retains the old receipt. Ambient pins cannot turn it into primary authority.
    conn.execute("DELETE FROM origin_native_receipts")
    conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(shadow))
    assert native.lookup(binding["origin"], binding["owner"], gateway="here") is None


def test_symlinked_primary_does_not_authorize(store, tmp_path):
    home, conn, _, binding = store
    conn.commit()
    primary = home / "kanban.db"
    relocated = tmp_path / "relocated.db"
    primary.rename(relocated)
    primary.symlink_to(relocated)
    assert native.lookup(binding["origin"], binding["owner"], gateway="here") is None


def test_local_owner_publication_preserves_receipt(store):
    _, conn, task, binding = store
    conn.execute("UPDATE tasks SET created_on='here' WHERE id=?", (task,))
    assert native._receipt(conn, task) == binding


def test_mode_roundtrip_does_not_resurrect_receipt(store):
    _, conn, task, _ = store
    conn.execute("UPDATE kanban_sync_state SET mode='shadow'")
    conn.execute("UPDATE kanban_sync_state SET mode='primary'")
    assert native._receipt(conn, task) is None


def test_readonly_lookup_needs_no_writer_initialization(store, monkeypatch):
    _, _, _, binding = store
    from hermes_cli import kanban_db_sync as sync
    monkeypatch.setattr(sync, "_SYNCED_PATHS", set())
    assert native.lookup(binding["origin"], binding["owner"], gateway="here") == binding

