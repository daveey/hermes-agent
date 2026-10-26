"""Ownership must not alter display identity, and must survive native moves."""
from contextlib import closing
from hermes_cli import kanban_db_connect as kbc
import json
import sqlite3
import tarfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_dispatch import DispatchResult
from hermes_cli.kanban_dispatch_owner import (
    get_dispatch_owner, metadata_path, run_owned_tick, set_dispatch_owner,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    return root


@pytest.mark.parametrize("layout", ["root-link", "db-link"])
def test_symlink_layout_preserves_discovery_and_display(home, tmp_path, layout):
    external = tmp_path / "storage"
    external.mkdir()
    if layout == "root-link":
        (home / "kanban").symlink_to(external, target_is_directory=True)
    else:
        directory = kb.board_dir("sample")
        directory.mkdir(parents=True)
        with sqlite3.connect(external / "board.db"):
            pass
        (directory / "kanban.db").symlink_to(external / "board.db")
    kb.create_board("sample", name="Display name", default_workdir=str(tmp_path), project_id="project")
    path = kb.board_dir("sample") / "kanban.db"
    set_dispatch_owner(path, "dedicated")
    assert "sample" in {row["slug"] for row in kb.list_boards(include_archived=False)}
    meta = kb.read_board_metadata("sample")
    assert (meta["name"], meta["default_workdir"], meta["project_id"]) == (
        "Display name", str(tmp_path), "project")
    kb.write_board_metadata("sample", description="Edited")
    assert get_dispatch_owner(path) == "dedicated"
    assert (kb.board_dir("sample") / "board.json").is_file()


@pytest.mark.parametrize("external", [False, True])
def test_archive_preserves_owner_for_direct_dispatch(home, tmp_path, external):
    kb.create_board("sample")
    path = kb.board_dir("sample") / "kanban.db"
    if external:
        destination = tmp_path / "external.db"
        path.rename(destination)
        path.symlink_to(destination)
    set_dispatch_owner(path, "dedicated")
    result = kb.remove_board("sample")
    archived = Path(result["new_path"]) / "kanban.db"
    assert get_dispatch_owner(archived) == "dedicated"
    with closing(kbc.connect(archived)) as conn:
        tick = Mock(return_value=DispatchResult())
        assert run_owned_tick(conn, tick, None, "sample", False).skipped_owned
        tick.assert_not_called()
    kb.create_board("sample")
    assert get_dispatch_owner(kb.board_dir("sample") / "kanban.db") is None


def test_dangling_owner_link_through_missing_ancestor_denies(home, tmp_path):
    path = home / "kanban.db"
    with closing(kbc.connect(path)) as conn:
        target_dir = tmp_path / "owner-data"
        target_dir.mkdir()
        target = target_dir / "owner.json"
        target.write_text('{"dispatch_owner":"dedicated"}')
        metadata = metadata_path(path)
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.symlink_to(target)
        assert get_dispatch_owner(path) == "dedicated"
        target_dir.rename(tmp_path / "moved-data")
        tick = Mock(return_value=DispatchResult())
        assert run_owned_tick(conn, tick, None, "default", False).skipped_owned
        tick.assert_not_called()


def test_moving_db_does_not_move_display_metadata(home, tmp_path):
    kb.create_board("sample", name="Original display", default_workdir=str(tmp_path), project_id="project")
    path = kb.board_dir("sample") / "kanban.db"
    target = tmp_path / "external.db"
    path.rename(target)
    path.symlink_to(target)
    meta = kb.read_board_metadata("sample")
    assert (meta["name"], meta["default_workdir"], meta["project_id"]) == (
        "Original display", str(tmp_path), "project")
    set_dispatch_owner(path, "dedicated")
    assert get_dispatch_owner(target) == "dedicated"


def test_export_import_does_not_transfer_ownership(home, tmp_path):
    from hermes_cli import kanban_transfer as transfer
    kb.create_board("sample")
    path = kb.board_dir("sample") / "kanban.db"
    set_dispatch_owner(path, "dedicated")
    # Even a manually-added display field must not leak in a portable archive.
    display = kb.board_metadata_path("sample")
    raw = json.loads(display.read_text())
    raw["dispatch_owner"] = "private-worker"
    display.write_text(json.dumps(raw))
    archive = transfer.export_board("sample", str(tmp_path / "export"))["archive"]
    with tarfile.open(archive) as bundle:
        assert not any(name.endswith(".dispatch-owner.json") for name in bundle.getnames())
        member = bundle.extractfile("sample/board.json")
        assert member is not None
        assert "dispatch_owner" not in json.load(member)
    transfer.import_board(archive, slug="imported")
    assert get_dispatch_owner(kb.board_dir("imported") / "kanban.db") is None
    assert get_dispatch_owner(path) == "dedicated"


def test_broken_display_ancestor_cannot_hide_owner(home, tmp_path):
    path = home / "kanban.db"
    display = kb.board_dir("default")
    display.parent.mkdir(parents=True)
    backing = tmp_path / "display"
    backing.mkdir()
    display.symlink_to(backing, target_is_directory=True)
    with closing(kbc.connect(path)) as conn:
        set_dispatch_owner(path, "dedicated")
        backing.rename(tmp_path / "moved-display")
        tick = Mock(return_value=DispatchResult())
        assert run_owned_tick(conn, tick, None, "default", False).skipped_owned
        tick.assert_not_called()
