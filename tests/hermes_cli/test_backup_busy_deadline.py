"""Backup progress deadlines and partial-destination cleanup."""

from pathlib import Path
import sqlite3


def test_aborts_when_source_remains_busy_past_deadline(tmp_path, monkeypatch, caplog):
    from hermes_cli import backup as backup_mod
    from hermes_cli import kanban_lockguard

    src = tmp_path / "locked.db"
    dst = tmp_path / "copy.db"
    src.touch()
    dst.write_bytes(b"partial")

    clock = iter((100.0, 100.5, 101.1))

    class FakeSourceConnection:
        def backup(self, _destination, *, pages, progress, sleep):
            assert pages > 0
            assert sleep > 0
            progress(sqlite3.SQLITE_BUSY, 0, 1)
            progress(sqlite3.SQLITE_BUSY, 0, 1)

        def close(self):
            pass

    destination_closed = []

    class FakeDestinationConnection:
        def close(self):
            destination_closed.append(True)

    connections = iter((FakeSourceConnection(), FakeDestinationConnection()))
    real_unlink = Path.unlink

    def assert_closed_before_unlink(path, *args, **kwargs):
        assert destination_closed
        return real_unlink(path, *args, **kwargs)

    connect_calls = []

    def fake_connect(*args, **kwargs):
        connect_calls.append((args, kwargs))
        return next(connections)

    # Inject busy progress at the backup's two opener boundaries, not
    # underneath the source guard's real SQL initialization.
    monkeypatch.setattr(kanban_lockguard, "connect", fake_connect)
    monkeypatch.setattr(backup_mod.sqlite3, "connect", fake_connect)
    monkeypatch.setattr(backup_mod.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(Path, "unlink", assert_closed_before_unlink)

    assert backup_mod._safe_copy_db(src, dst, timeout_seconds=1.0) is False
    assert "database remained locked for 1 seconds" in caplog.text
    assert connect_calls[0][1]["timeout"] == 0.0
    assert not dst.exists()
