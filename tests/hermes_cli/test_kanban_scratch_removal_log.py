"""A scratch workspace removed by card completion is logged at INFO with the
task, its last run and the removing pid, so an agent that later finds its cwd
gone (checkpoint "working directory not found") can be traced to who removed it."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_completion_logs_scratch_removal_with_task_and_run(kanban_home, caplog):
    caplog.set_level(logging.INFO, logger="hermes_cli.kanban_db")
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="scratch removal")
        ws = kbw.resolve_workspace(kb.get_task(conn, t))
        kbw.set_workspace_path(conn, t, ws)
        assert kb.complete_task(conn, t, result="ok")
        run = kb.latest_run(conn, t)

    assert not ws.exists()
    lines = [r.getMessage() for r in caplog.records
             if r.levelno == logging.INFO and r.getMessage().startswith("Removed scratch workspace")]
    assert len(lines) == 1, lines
    assert f"for task {t}," in lines[0]
    assert f"last run {run.id} ({run.status})" in lines[0]
    assert f"by pid {os.getpid()}" in lines[0]
    assert lines[0].endswith(str(ws))
