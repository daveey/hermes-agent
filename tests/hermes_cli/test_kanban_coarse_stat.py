"""SQLite commit invalidation must not depend on timestamp resolution."""
import sqlite3

import pytest

from hermes_cli import kanban_activity_revision as rev
from hermes_cli import kanban_session_activity as ksa


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_legacy_commit_key_tracks_writes_and_reader_replacement(tmp_path, monkeypatch, journal):
    path = tmp_path / "legacy.db"
    ksa._reset_cache_for_tests()
    writer = sqlite3.connect(path)
    try:
        writer.execute(f"PRAGMA journal_mode={journal}")
        writer.executescript("CREATE TABLE tasks (title TEXT); INSERT INTO tasks VALUES ('one');")
        frozen = rev.file_stat(path)
        monkeypatch.setattr(rev, "file_stat", lambda _path: frozen)
        first = ksa._board_content_key(path)
        assert ksa._board_content_key(path) == first
        writer.execute("UPDATE tasks SET title='two'")
        writer.commit()
        second = ksa._board_content_key(path)
        assert second != first
        assert ksa._board_content_key(path) == second
        ksa._reset_cache_for_tests()  # new connections can repeat data_version values
        third = ksa._board_content_key(path)
        assert third != second
        assert ksa._board_content_key(path) == third
    finally:
        writer.close()
        ksa._reset_cache_for_tests()
