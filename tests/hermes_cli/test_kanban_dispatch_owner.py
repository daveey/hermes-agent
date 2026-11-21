"""Board dispatch ownership is coordination, enforced before any tick effects."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board_db(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    path = home / "kanban.db"
    metadata = path.with_name(path.name + ".dispatch-owner.json")
    conn = kbc.connect(path)
    yield conn, path, metadata
    conn.close()


def _metadata(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _deny_without_effects(conn, monkeypatch, **kwargs):
    """The entire DB, not only the candidate card, must remain untouched."""
    before = list(conn.iterdump())
    changes = conn.total_changes
    spawn = Mock(side_effect=AssertionError("denied dispatch spawned a worker"))
    # Reaping is a process side effect even when it leaves the DB unchanged.
    reapers = []
    for name in ("reap_worker_zombies", "reap_terminal_workers"):
        spy = Mock(side_effect=AssertionError("denied dispatch reaped workers"))
        monkeypatch.setattr(kbd, name, spy)
        reapers.append(spy)
    result = kbd.dispatch_once(conn, spawn_fn=spawn, **kwargs)
    assert result.skipped_owned is True
    assert result.spawned == []
    assert list(conn.iterdump()) == before
    assert conn.total_changes == changes
    spawn.assert_not_called()
    for spy in reapers:
        spy.assert_not_called()
    return result


@pytest.mark.parametrize("identity", [None, "other-worker", "", "OWNER", "owner\n"])
def test_owned_board_denies_default_mismatch_and_invalid_identity(board_db, monkeypatch, identity):
    conn, _, metadata = board_db
    _metadata(metadata, {"dispatch_owner": "owner"})
    promotable = kb.create_task(conn, title="Promotable card", assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (promotable,))
    stale = kb.create_task(conn, title="Expired claim", assignee="default")
    assert kb.claim_task(conn, stale, ttl_seconds=-1)
    result = _deny_without_effects(conn, monkeypatch, dispatcher_id=identity)
    assert result.dispatch_owner == "owner"


@pytest.mark.parametrize("raw", [
    '{"dispatch_owner": null}', '{"dispatch_owner": ""}',
    '{"dispatch_owner": 1}', '{"dispatch_owner": false}',
    '{"dispatch_owner": []}', '{"dispatch_owner": {}}',
    '{"dispatch_owner": "Owner"}', '{"dispatch_owner": "owner\\n"}',
    '{"dispatch_owner": " owner"}', '{"dispatch_owner": "a/b"}',
    '{"dispatch_owner": "-owner"}', '{broken', '[]', 'null',
])
def test_invalid_metadata_denies_and_strict_reader_raises(board_db, monkeypatch, raw):
    from hermes_cli.kanban_dispatch_owner import get_dispatch_owner

    conn, path, metadata = board_db
    metadata.write_text(raw, encoding="utf-8")
    kb.create_task(conn, title="Must not promote", assignee="default")
    with pytest.raises((ValueError, OSError, RuntimeError)):
        get_dispatch_owner(path)
    result = _deny_without_effects(conn, monkeypatch, dispatcher_id="owner")
    assert result.ownership_error


@pytest.mark.parametrize("kind", ["missing", "unowned", "owned"])
def test_allowed_tick_really_claims_and_spawns(board_db, monkeypatch, kind):
    conn, _, metadata = board_db
    if kind != "missing":
        _metadata(metadata, {"dispatch_owner": "owner"} if kind == "owned" else {})
    # Only OS process effects are replaced: claim, promotion, workspace and run
    # persistence remain real, inside the temporary home.
    monkeypatch.setattr(kbd, "reap_worker_zombies", lambda: None)
    monkeypatch.setattr(kbd, "reap_terminal_workers", lambda conn: [])
    tid = kb.create_task(conn, title="Dispatchable card", assignee="default")
    spawn = Mock(return_value=None)
    preview = kbd.dispatch_once(conn, dispatcher_id="owner" if kind == "owned" else None,
                                spawn_fn=spawn, reconcile_orphans=False, dry_run=True)
    assert preview.skipped_ineligible == [(tid, "model_unroutable:routing_disabled")]
    assert not preview.spawned and not kb.list_runs(conn, tid)
    spawn.assert_not_called()
    from hermes_cli.kanban_model_assignment import _assignee_config
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    home = Path.home() / "home"
    (home / "config.yaml").write_text(_managed_yaml())
    assert _assignee_config("default")["model_routing"]["enabled"]
    result = kbd.dispatch_once(conn, dispatcher_id="owner" if kind == "owned" else None,
                               spawn_fn=spawn, reconcile_orphans=False)
    assert result.skipped_owned is False
    assert result.ownership_error is None
    assert result.dispatch_owner == ("owner" if kind == "owned" else None)
    assert not result.respawn_guarded, result.respawn_guarded
    assert [row[0] for row in result.spawned] == [tid], result
    assert spawn.call_count == 1
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "running"
    assert task.current_run_id is not None


@pytest.mark.parametrize("alias", ["explicit", "pinned", "both"])
def test_connection_path_not_board_alias_controls_ownership(board_db, tmp_path, monkeypatch, alias):
    default_conn, default_path, metadata = board_db
    _metadata(metadata, {})
    actual = tmp_path / "actual" / "board.db"
    actual.parent.mkdir()
    _metadata(actual.with_name(actual.name + ".dispatch-owner.json"), {"dispatch_owner": "owner"})
    if alias in ("pinned", "both"):
        monkeypatch.setenv("HERMES_KANBAN_DB", str(default_path))
    with kbc.connect_closing(actual) as conn:
        kb.create_task(conn, title="Owned despite alias", assignee="default")
        result = _deny_without_effects(
            conn, monkeypatch, dispatcher_id="other-worker",
            **({"board": "default"} if alias in ("explicit", "both") else {}),
        )
    assert result.dispatch_owner == "owner"
    assert default_conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_metadata_read_error_fails_closed(board_db, monkeypatch):
    from hermes_cli.kanban_dispatch_owner import get_dispatch_owner

    conn, path, metadata = board_db
    # Deterministic read failure even under root; chmod-based tests are not.
    metadata.mkdir()
    with pytest.raises((OSError, ValueError, RuntimeError)):
        get_dispatch_owner(path)
    result = _deny_without_effects(conn, monkeypatch, dispatcher_id="owner")
    assert result.ownership_error


@pytest.mark.parametrize("layout", ["default", "adjacent"])
def test_set_read_clear_preserves_other_metadata(board_db, tmp_path, layout):
    from hermes_cli.kanban_dispatch_owner import get_dispatch_owner, set_dispatch_owner

    _, path, metadata = board_db
    if layout == "adjacent":
        path = tmp_path / "custom" / "board.db"
        metadata = path.with_name(path.name + ".dispatch-owner.json")
    _metadata(metadata, {"title": "Generic board", "custom": {"keep": True}})
    with kbc.connect_closing(path):
        pass
    set_dispatch_owner(path, "worker-1")
    assert get_dispatch_owner(path) == "worker-1"
    saved = json.loads(metadata.read_text(encoding="utf-8"))
    assert saved["title"] == "Generic board"
    assert saved["custom"] == {"keep": True}
    set_dispatch_owner(path, None)
    assert get_dispatch_owner(path) is None
    assert "dispatch_owner" not in json.loads(metadata.read_text(encoding="utf-8"))


@pytest.mark.parametrize("owner", ["", "Owner", "owner\n", " owner", "a/b", 42, False, [], {}])
def test_setter_rejects_invalid_owner_without_changing_metadata(board_db, owner):
    from hermes_cli.kanban_dispatch_owner import set_dispatch_owner

    _, path, metadata = board_db
    _metadata(metadata, {"dispatch_owner": "owner", "title": "Keep"})
    before = metadata.read_bytes()
    with pytest.raises((TypeError, ValueError)):
        set_dispatch_owner(path, owner)
    assert metadata.read_bytes() == before


@pytest.mark.parametrize("new_owner", ["other-worker", None])
def test_setter_cannot_change_owner_while_dispatch_lock_held(board_db, new_owner):
    from hermes_cli.kanban_dispatch_owner import get_dispatch_owner, set_dispatch_owner

    _, path, metadata = board_db
    _metadata(metadata, {"dispatch_owner": "owner"})
    with kbc._dispatch_tick_lock(path) as held:
        assert held
        with pytest.raises(RuntimeError):
            set_dispatch_owner(path, new_owner)
        assert get_dispatch_owner(path) == "owner"
    set_dispatch_owner(path, new_owner)
    assert get_dispatch_owner(path) == new_owner


def test_real_pinned_resolution_and_foreign_default(board_db, tmp_path, monkeypatch):
    _, _, _ = board_db
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    path = foreign / "kanban.db"
    _metadata(path.with_name(path.name + ".dispatch-owner.json"),
              {"dispatch_owner": "foreign-worker"})
    with kbc.connect_closing(path):
        pass
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    with kb.pin_first_board_resolution():
        with kbc.connect_closing(board="default") as conn:
            result = _deny_without_effects(conn, monkeypatch, board="default")
            assert result.dispatch_owner == "foreign-worker"


def test_gateway_dispatch_cannot_bypass_ownership(board_db, monkeypatch):
    from gateway.kanban_watchers_dispatcher import _DispatcherSettings, _KanbanDispatcher

    conn, _, metadata = board_db
    _metadata(metadata, {"dispatch_owner": "owner"})
    kb.create_task(conn, title="Gateway must leave alone", assignee="default")
    before = list(conn.iterdump())
    spawn = Mock(side_effect=AssertionError("gateway spawned on owned board"))
    monkeypatch.setattr(kbd, "_default_spawn", spawn)
    settings = _DispatcherSettings(
        interval=60, max_spawn=None, max_in_progress=None, failure_limit=2,
        stale_timeout_seconds=0, reconcile_orphans=True,
        default_assignee=None, max_in_progress_per_profile=None,
    )
    dispatcher = _KanbanDispatcher(kb, settings)
    result = dispatcher.tick_once_for_board("default")
    assert result is not None
    assert result.skipped_owned is True
    assert result.scheduling_admitted is False
    assert dispatcher.successful_scopes == []
    assert result.dispatch_owner == "owner"
    assert list(conn.iterdump()) == before
    spawn.assert_not_called()


def test_cli_dispatch_cannot_bypass_ownership(board_db, monkeypatch):
    from hermes_cli import kanban as cli

    conn, _, metadata = board_db
    _metadata(metadata, {"dispatch_owner": "owner"})
    kb.create_task(conn, title="CLI must leave alone", assignee="default")
    before = list(conn.iterdump())
    spawn = Mock(side_effect=AssertionError("CLI spawned on owned board"))
    monkeypatch.setattr(kbd, "_default_spawn", spawn)
    cli._cmd_dispatch(argparse.Namespace(dry_run=False, max=None, failure_limit=2, json=True))
    assert list(conn.iterdump()) == before
    spawn.assert_not_called()
