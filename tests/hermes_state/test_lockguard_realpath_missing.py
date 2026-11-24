"""Non-strict path identity must survive deleted databases and symlink targets."""
import os

import pytest

import hermes_state_lockguard_fdscan as fdscan

pytestmark = pytest.mark.platforms("linux")


def test_missing_realpath_preserves_symlink_and_dot_semantics(tmp_path, monkeypatch):
    if fdscan.held_realpath(str(tmp_path)) is None:
        pytest.skip("native realpath unavailable")
    (tmp_path / "alias").symlink_to("missing/target.db")
    (tmp_path / "absolute").symlink_to(tmp_path / "missing" / "target.db")
    (tmp_path / "chain").symlink_to("alias")
    (tmp_path / "loop").symlink_to("loop")
    (tmp_path / "a").symlink_to("b")
    (tmp_path / "b").symlink_to("a")
    monkeypatch.chdir(tmp_path)
    for name in ("alias", "absolute", "chain", "alias/../board.db", "missing/../board.db",
                 "missing/./tail", "loop/tail", "a/tail", "chain/../../chain"):
        assert fdscan.held_realpath(name) == os.path.realpath(name), name


def test_dangling_database_alias_finds_held_deleted_wal(tmp_path):
    from hermes_cli import kanban_lockguard

    target = tmp_path / "target.db"
    alias = tmp_path / "alias.db"
    alias.symlink_to(target)
    wal = tmp_path / "target.db-wal"
    fd = os.open(wal, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        wal.unlink()
        found = kanban_lockguard.stranded_sidecars(alias)
        assert any(str(wal) in item for item in found), found
    finally:
        os.close(fd)
