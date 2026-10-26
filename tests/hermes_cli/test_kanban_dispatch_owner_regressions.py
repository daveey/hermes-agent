"""Ownership authority must be invariant across homes and DB aliases."""
import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_dispatch_owner import get_dispatch_owner, run_owned_tick, set_dispatch_owner
from hermes_cli.kanban_db_dispatch import DispatchResult


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    return root


def test_foreign_default_owner_survives_home_switch_and_display_edit(home, tmp_path, monkeypatch):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    path = foreign / "kanban.db"
    with sqlite3.connect(path) as conn:
        set_dispatch_owner(path, "dedicated")
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(foreign))
        tick = Mock(return_value=DispatchResult())
        assert run_owned_tick(conn, tick, None, "default", False).skipped_owned
        tick.assert_not_called()
        kb.write_board_metadata("default", description="Edited")
        assert get_dispatch_owner(path) == "dedicated"
        assert kb.read_board_metadata("default")["description"] == "Edited"


def test_default_symlink_to_named_board_cannot_hide_owner(home, tmp_path, monkeypatch):
    path = tmp_path / "foreign" / "kanban" / "boards" / "sample" / "kanban.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path):
        pass
    set_dispatch_owner(path, "dedicated")
    (home / "kanban.db").symlink_to(path)
    with sqlite3.connect(home / "kanban.db") as conn:
        tick = Mock(return_value=DispatchResult())
        assert run_owned_tick(conn, tick, None, "default", False).skipped_owned
        tick.assert_not_called()
    assert get_dispatch_owner(home / "kanban.db") == "dedicated"
    kb.write_board_metadata("default", description="Alias edit")
    assert get_dispatch_owner(path) == "dedicated"


def test_distinct_db_filenames_never_share_owner(home):
    paths = [home / "first.db", home / "second.db"]
    for path in paths:
        with sqlite3.connect(path):
            pass
    set_dispatch_owner(paths[0], "first")
    assert get_dispatch_owner(paths[1]) is None
    set_dispatch_owner(paths[1], "second")
    assert [get_dispatch_owner(p) for p in paths] == ["first", "second"]


def test_deep_metadata_denies_without_traceback(home):
    path = home / "kanban.db"
    metadata = path.with_name(path.name + ".dispatch-owner.json")
    metadata.write_text('[' * 2000 + ']' * 2000)
    with sqlite3.connect(path) as conn:
        tick = Mock()
        result = run_owned_tick(conn, tick, None, "default", False)
        assert result.skipped_owned and result.ownership_error
        tick.assert_not_called()


@pytest.mark.parametrize("owned", [True, False])
def test_lock_failure_is_closed_even_for_unowned_board(home, monkeypatch, owned):
    path = home / "kanban.db"
    with sqlite3.connect(path) as conn:
        if owned:
            set_dispatch_owner(path, "dedicated")
        original = Path.open
        def fail_lock(p, *args, **kwargs):
            if p.name.endswith(".dispatch.lock"):
                raise PermissionError("lock unavailable")
            return original(p, *args, **kwargs)
        monkeypatch.setattr(Path, "open", fail_lock)
        tick = Mock()
        result = run_owned_tick(conn, tick, None, "default", False)
        assert result.skipped_locked
        tick.assert_not_called()


@pytest.mark.parametrize("raw", [{"dispatch_owner": "dedicated"}, {"dispatch_owner": None}, {}])
def test_gateway_decompose_and_health_obey_owner(home, monkeypatch, raw):
    from gateway.kanban_watchers_dispatcher import _DispatcherSettings, _KanbanDispatcher
    from hermes_cli import kanban_decompose as decomp, kanban_db_dispatch as kbd
    kb.create_board("sample")
    path = kb.board_dir("sample") / "kanban.db"
    metadata = path.with_name(path.name + ".dispatch-owner.json")
    metadata.write_text(json.dumps(raw))
    settings = _DispatcherSettings(60, None, None, 2, 0, True, None, None)
    dispatcher = _KanbanDispatcher(kb, settings)
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["sample"])
    listed = Mock(return_value=["test-card"])
    decomposed = Mock(return_value=1)
    monkeypatch.setattr(decomp, "list_triage_ids", listed)
    monkeypatch.setattr(dispatcher, "_decompose_one", decomposed)
    monkeypatch.setattr(kbd, "has_spawnable_ready", lambda conn: True)
    permitted = not raw
    assert dispatcher.auto_decompose_tick(1) == int(permitted)
    assert dispatcher.ready_nonempty() is permitted
    assert listed.call_count == int(permitted)
    assert decomposed.call_count == int(permitted)


def test_gateway_holds_owner_lock_through_decompose(home, monkeypatch):
    from gateway.kanban_watchers_dispatcher import _DispatcherSettings, _KanbanDispatcher
    from hermes_cli import kanban_decompose as decomp
    kb.create_board("sample")
    path = kb.kanban_db_path("sample")
    dispatcher = _KanbanDispatcher(kb, _DispatcherSettings(60, None, None, 2, 0, True, None, None))
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["sample"])
    monkeypatch.setattr(decomp, "list_triage_ids", lambda **kwargs: ["test-card"])
    def decompose(*args):
        with pytest.raises(RuntimeError):
            set_dispatch_owner(path, "dedicated")
        return 1
    monkeypatch.setattr(dispatcher, "_decompose_one", decompose)
    assert dispatcher.auto_decompose_tick(1) == 1
    set_dispatch_owner(path, "dedicated")
    assert dispatcher.auto_decompose_tick(1) == 0


def test_reserved_default_directory_db_does_not_share_root_owner(home):
    root = home / "kanban.db"
    nested = home / "kanban/boards/default/kanban.db"
    nested.parent.mkdir(parents=True)
    for path in (root, nested):
        with sqlite3.connect(path):
            pass
    set_dispatch_owner(root, "root-owner")
    assert get_dispatch_owner(nested) is None
    set_dispatch_owner(nested, "nested-owner")
    assert get_dispatch_owner(root) == "root-owner"


@pytest.mark.parametrize("archive", [True, False])
def test_remove_busy_board_leaves_directory_and_current_intact(home, archive):
    kb.create_board("sample")
    kb.set_current_board("sample")
    path = kb.kanban_db_path("sample")
    with kbc._dispatch_tick_lock(path) as held:
        assert held
        with pytest.raises(ValueError, match="busy"):
            kb.remove_board("sample", archive=archive)
        assert kb.get_current_board() == "sample"
        assert path.exists()
        assert not kb.read_board_metadata("sample")["archived"]
    kb.remove_board("sample", archive=archive)
    assert kb.get_current_board() == "default"
