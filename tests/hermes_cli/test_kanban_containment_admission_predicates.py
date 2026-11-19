"""Actor adapter boundaries isolated from lower-level defensive validation."""
from __future__ import annotations

import sqlite3

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_cli import kanban_containment_actor as actors
from hermes_cli import kanban_containment_connections as connections
from hermes_cli import kanban_containment_lifecycle as lifecycle
from hermes_cli import kanban_containment_service as service
from tests.hermes_cli import test_kanban_containment_actor as fixtures
from tests.hermes_cli import test_kanban_containment_lifecycle as local_fixtures

board = fixtures.board


@pytest.mark.parametrize("key", ["HERMES_KANBAN_DB", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_TASK", "HERMES_SESSION_ID"])
def test_missing_pin_refuses_before_lookup_even_if_lower_actor_is_stubbed(board, monkeypatch, key):
    _, _, session = fixtures.bind(board, monkeypatch)
    monkeypatch.setattr(actors, "_actor", lambda *_: None)
    with scoped_current_session_id(session):
        assert actors.current_actor(board).run_uid
        monkeypatch.delenv(key)
        with scoped_current_session_id("" if key == "HERMES_SESSION_ID" else session):
            statements = []
            board.set_trace_callback(statements.append)
            try:
                with pytest.raises(lifecycle.ContainmentRefused, match="dispatcher-admitted"):
                    actors.current_actor(board)
            finally:
                board.set_trace_callback(None)
            assert not any("FROM task_runs WHERE" in sql for sql in statements)


def test_actor_requires_file_backed_pinned_board(board, monkeypatch):
    _, _, session = fixtures.bind(board, monkeypatch)
    from pathlib import Path
    monkeypatch.setenv("HERMES_KANBAN_DB", str(Path.cwd()))
    with sqlite3.connect(":memory:") as memory, scoped_current_session_id(session):
        with pytest.raises(lifecycle.ContainmentRefused, match="dispatcher-admitted"):
            actors.current_actor(memory)
    monkeypatch.setenv("HERMES_KANBAN_DB", "/fixture/not-this-board.db")
    with scoped_current_session_id(session), pytest.raises(lifecycle.ContainmentRefused, match="dispatcher-admitted"):
        actors.current_actor(board)


@pytest.mark.parametrize("run_id", ["bad", "١", "1.0"])
def test_run_id_validation_refuses_at_admission_boundary(board, monkeypatch, run_id):
    _, _, session = fixtures.bind(board, monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
    with scoped_current_session_id(session), pytest.raises(lifecycle.ContainmentRefused, match="dispatcher-admitted"):
        actors.current_actor(board)


def test_adapter_rechecks_live_actor_after_adoption(board, monkeypatch):
    task, run, session = fixtures.bind(board, monkeypatch)
    with scoped_current_session_id(session):
        assert actors.current_actor(board).task == task
        board.execute("UPDATE task_runs SET ended_at=1 WHERE id=?", (run,))
        with pytest.raises(lifecycle.ContainmentRefused, match="current live claimed run"):
            actors.current_actor(board)


def test_target_independently_rejects_persisted_sync_membership(board, monkeypatch):
    task, _, _ = fixtures.bind(board, monkeypatch)
    assert service._target(board, task)["task_id"] == task
    board.execute("CREATE TABLE kanban_sync_state(fixture TEXT)")
    board.execute("INSERT INTO kanban_sync_state VALUES ('fixture')")
    with pytest.raises(lifecycle.ContainmentRefused, match="synced containment capability"):
        service._target(board, task)


def test_local_bootstrap_cannot_manufacture_synced_identity(board):
    task, _, _, _, _ = local_fixtures.setup(board)
    run = board.execute("SELECT current_run_id FROM tasks WHERE id=?", (task,)).fetchone()[0]
    board.execute("UPDATE task_runs SET uid=NULL WHERE id=?", (run,))
    board.execute("CREATE TABLE kanban_sync_state(fixture TEXT)")
    board.execute("INSERT INTO kanban_sync_state VALUES ('fixture')")
    with pytest.raises(lifecycle.ContainmentRefused, match="synced containment capability"):
        connections.bootstrap(board)
    assert board.execute("SELECT uid FROM task_runs WHERE id=?", (run,)).fetchone()[0] is None
