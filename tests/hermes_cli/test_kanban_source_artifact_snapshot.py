"""File selections bind regular opened bytes, not a pre-open pathname guess."""
import io
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_source_artifacts as artifacts
from tests.hermes_cli import test_kanban_sync_attachments as transport

stores = transport.stores


@pytest.mark.platforms("posix")
def test_leaf_symlink_is_not_implicit_selection(stores, tmp_path):
    _, _, _, conn, _, tid, _ = stores
    target = tmp_path / "real.txt"
    target.write_bytes(b"evidence")
    alias = tmp_path / "alias.txt"
    alias.symlink_to(target)
    with pytest.raises(artifacts.ArtifactRefused, match="select a regular file, not a leaf symlink or special file"):
        artifacts.stage(conn, tid, [str(alias)], board="default", run_uid="one")
    assert artifacts.selected(conn, tid, "one") is None
    assert artifacts.stage(conn, tid, [str(target)], board="default", run_uid="one")


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("replacement", ["fifo", "symlink", "regular"])
def test_replacement_at_open_never_enters_selection(stores, tmp_path, monkeypatch, replacement):
    _, _, _, conn, _, tid, _ = stores
    target = tmp_path / "selected.txt"
    target.write_bytes(b"evidence")
    other = tmp_path / "other.txt"
    other.write_bytes(b"replacement")
    io_open, os_open = io.open, os.open
    changed = []

    def replace(path):
        if not isinstance(path, int) and Path(path) == target and not changed:
            changed.append(True)
            target.rename(tmp_path / "original.txt")
            if replacement == "fifo":
                os.mkfifo(target)
            elif replacement == "symlink":
                target.symlink_to(other)
            else:
                target.write_bytes(b"replacement")

    def high_open(path, *args, **kwargs):
        replace(path)
        if not isinstance(path, int) and Path(path) == target and replacement == "fifo":
            pytest.fail("blocking file open would hang on the replacement FIFO")
        return io_open(path, *args, **kwargs)

    def low_open(path, flags, *args, **kwargs):
        replace(path)
        if Path(path) == target and replacement == "fifo" and not flags & os.O_NONBLOCK:
            pytest.fail("blocking descriptor open would hang on the replacement FIFO")
        return os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(io, "open", high_open)
    monkeypatch.setattr(os, "open", low_open)
    with pytest.raises(artifacts.ArtifactRefused):
        artifacts.stage(conn, tid, [str(target)], board="default", run_uid="one")
    assert changed == [True]
    assert artifacts.selected(conn, tid, "one") is None


def test_filename_credential_is_not_published(stores, tmp_path):
    _, _, _, conn, _, tid, _ = stores
    target = tmp_path / "-----BEGIN PRIVATE KEY-----.txt"
    target.write_bytes(b"innocent contents")
    with pytest.raises(artifacts.ArtifactRefused, match="filename"):
        artifacts.stage(conn, tid, [str(target)], board="default", run_uid="one")
    assert artifacts.selected(conn, tid, "one") is None
