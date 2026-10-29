"""A finished run's HERMES_KANBAN_TASK / HERMES_KANBAN_RUN_ID is not a worker. [daveey]

cmux keeps a dead worker's env forever (login -flp). Hermes must drop that pair at
process start, before plugins load, unless the run is still live on this machine's
board. An unreadable board is kept: don't guess.
"""
from __future__ import annotations

import inspect
import logging
import os
import sqlite3

import pytest

from hermes_cli.kanban_identity import drop_finished_kanban_identity

TASK = "t_deadbeef"
RUN = 5045


def _board(path, *, task=TASK, run_id=RUN, live=False, claim=True, task_status=None, run_status=None):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, claim_lock TEXT, current_run_id INTEGER)"
    )
    conn.execute(
        "CREATE TABLE task_runs ("
        "id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, claim_lock TEXT, ended_at INTEGER)"
    )
    if live:
        conn.execute(
            "INSERT INTO tasks VALUES (?, 'running', 'lock-1', ?)", (task, run_id),
        )
        conn.execute(
            "INSERT INTO task_runs VALUES (?, ?, 'running', 'lock-1', NULL)", (run_id, task),
        )
    else:
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?)",
            (task, task_status or "done", "lock-1" if claim else None, run_id if claim else None),
        )
        conn.execute(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?)",
            (run_id, task, run_status or "done", "lock-1" if claim else None, 1793082185),
        )
    conn.commit()
    conn.close()
    return path


def _pin(monkeypatch, db, task=TASK, run_id=str(RUN)):
    monkeypatch.setenv("HERMES_KANBAN_TASK", task)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_WORKER_TASK", "t_keep_me")


def test_finished_run_is_dropped_and_not_a_worker(monkeypatch, tmp_path, caplog):
    db = _board(tmp_path / "kanban.db", live=False)
    _pin(monkeypatch, db)
    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_identity"):
        removed = drop_finished_kanban_identity()
    assert removed == ["HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"]
    assert "HERMES_KANBAN_TASK" not in os.environ
    assert "HERMES_KANBAN_RUN_ID" not in os.environ
    assert os.environ["HERMES_KANBAN_WORKER_TASK"] == "t_keep_me"
    from tools.tool_search import _in_kanban_worker
    assert _in_kanban_worker() is False
    assert "dropped finished kanban identity" in caplog.text
    assert TASK in caplog.text and str(RUN) in caplog.text


def test_live_run_is_kept_and_is_a_worker(monkeypatch, tmp_path, caplog):
    db = _board(tmp_path / "kanban.db", live=True)
    _pin(monkeypatch, db)
    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_identity"):
        assert drop_finished_kanban_identity() == []
    assert os.environ["HERMES_KANBAN_TASK"] == TASK
    assert os.environ["HERMES_KANBAN_RUN_ID"] == str(RUN)
    from tools.tool_search import _in_kanban_worker
    assert _in_kanban_worker() is True
    assert "dropped finished kanban identity" not in caplog.text


def test_unreadable_board_keeps_the_identity(monkeypatch, tmp_path, caplog):
    missing = tmp_path / "no-such-dir" / "kanban.db"
    _pin(monkeypatch, missing)
    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_identity"):
        assert drop_finished_kanban_identity() == []
    assert os.environ["HERMES_KANBAN_TASK"] == TASK
    assert "dropped finished kanban identity" not in caplog.text


def test_run_for_a_different_task_is_dropped(monkeypatch, tmp_path):
    db = _board(tmp_path / "kanban.db", live=True, task="t_other")
    _pin(monkeypatch, db, task=TASK)
    assert drop_finished_kanban_identity() == ["HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"]
    assert "HERMES_KANBAN_TASK" not in os.environ


def test_bare_task_without_a_run_id_is_dropped(monkeypatch, tmp_path):
    db = _board(tmp_path / "kanban.db", live=True)
    monkeypatch.setenv("HERMES_KANBAN_TASK", TASK)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    assert drop_finished_kanban_identity() == ["HERMES_KANBAN_TASK"]
    assert "HERMES_KANBAN_TASK" not in os.environ


def test_run_that_does_not_hold_a_claim_is_dropped(monkeypatch, tmp_path):
    """ended_at NULL is not enough: the row must still hold a running claim."""
    path = tmp_path / "kanban.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, claim_lock TEXT, current_run_id INTEGER)")
    conn.execute(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, claim_lock TEXT, ended_at INTEGER)"
    )
    conn.execute("INSERT INTO tasks VALUES (?, 'running', NULL, ?)", (TASK, RUN))
    conn.execute("INSERT INTO task_runs VALUES (?, ?, 'running', NULL, NULL)", (RUN, TASK))
    conn.commit()
    conn.close()
    _pin(monkeypatch, path)
    assert drop_finished_kanban_identity() == ["HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"]
    assert "HERMES_KANBAN_TASK" not in os.environ


def test_noop_when_unset(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    assert drop_finished_kanban_identity() == []


def test_main_drops_identity_before_plugins_load():
    from hermes_cli.main import main
    src = inspect.getsource(main)
    drop_at = src.index("drop_finished_kanban_identity")
    assert drop_at < src.index("_prepare_agent_startup")
    assert drop_at < src.index("_try_fast_chat_launch")
    assert drop_at < src.index("_try_fast_serve_launch")
