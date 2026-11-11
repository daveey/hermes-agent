"""SessionDB normalizes filesystem paths before guards and SQLite setup."""
from pathlib import Path

import pytest

import hermes_state
from hermes_state import SessionDB


class StringPath:
    def __init__(self, path):
        self.path = str(path)

    def __fspath__(self):
        return self.path


@pytest.mark.parametrize("convert", [str, Path, StringPath])
@pytest.mark.parametrize("read_only", [False, True])
def test_filesystem_path_round_trip(tmp_path, convert, read_only):
    path = tmp_path / "nested" / "sessions.db"
    writer = SessionDB(path)
    writer.create_session("path-roundtrip", "test")
    writer.close()
    db = SessionDB(convert(path), read_only=read_only)
    try:
        assert db.db_path == path
        assert isinstance(db.db_path, Path)
        session = db.get_session("path-roundtrip")
        assert session is not None
        assert session["source"] == "test"
    finally:
        db.close()


def test_string_path_creates_missing_parent(tmp_path):
    path = tmp_path / "new-parent" / "sessions.db"
    db = SessionDB(str(path))
    try:
        assert path.is_file()
    finally:
        db.close()


def test_default_path_is_preserved(tmp_path, monkeypatch):
    path = tmp_path / "default.db"
    monkeypatch.setattr(hermes_state, "_default_db_path", lambda: path)
    db = SessionDB()
    try:
        assert db.db_path == path
    finally:
        db.close()


def test_isolation_guard_receives_normalized_path(tmp_path, monkeypatch):
    path = tmp_path / "guarded.db"
    seen = []

    def reject(candidate):
        seen.append(candidate)
        raise RuntimeError("isolation sentinel")

    monkeypatch.setattr(hermes_state, "_ensure_test_isolation", reject)
    with pytest.raises(RuntimeError, match="isolation sentinel"):
        SessionDB(str(path))
    assert seen == [path]
    assert not path.exists()
