"""Raw forensic copies must hold the connection-lifecycle fence, not just check it."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli.sqlite_safe_read import LiveConnectionError, connect_tracked
from hermes_state_repair import _publish_backup_bundle


def test_copy_rejects_connection_opened_after_preflight(tmp_path):
    db = tmp_path / "state.db"
    conn = connect_tracked(db)
    try:
        with pytest.raises(LiveConnectionError):
            _publish_backup_bundle(db, tmp_path / "staged", tmp_path / "backup")
        assert not (tmp_path / "backup").exists()
    finally:
        conn.close()


def test_copy_holds_fence_against_concurrent_opener(tmp_path, monkeypatch):
    import hermes_state_repair as repair

    db = tmp_path / "state.db"
    conn = connect_tracked(db)
    conn.execute("CREATE TABLE sample (value)")
    conn.close()
    original = repair.shutil.copy2
    probes = []

    def attempt_open():
        try:
            peer = connect_tracked(db, wait_for_tracking=False)
        except sqlite3.OperationalError:
            return "fenced"
        peer.close()
        return "opened"

    with ThreadPoolExecutor(max_workers=1) as pool:
        def checked_copy(src, dst):
            result = pool.submit(attempt_open).result(timeout=5)
            probes.append(result)
            assert result == "fenced"
            return original(src, dst)

        monkeypatch.setattr(repair.shutil, "copy2", checked_copy)
        _publish_backup_bundle(db, tmp_path / "staged", tmp_path / "backup")
    assert probes == ["fenced"]
    restored = sqlite3.connect(tmp_path / "backup")
    try:
        assert restored.execute("SELECT * FROM sample").fetchall() == []
    finally:
        restored.close()
