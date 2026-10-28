"""`hermes kanban --board B` from a Kanban worker's terminal, and `boards list` counts. [daveey]

A worker's terminal subprocesses inherit ``HERMES_KANBAN_DB`` (the worker's own board) and the
delegate fence marker ``HERMES_DELEGATED_CHILD_CONTEXT`` (the lineage's board root). The fence
used to make every explicit ``--board`` resolve through the pin, so
``hermes kanban --board runners show <id>`` read the worker's own board and printed
"no such task", and ``boards list`` printed the pinned board's counts on every row.

Now an explicit board wins for such a descendant whenever the board's own DB is still inside the
fenced root, so reads follow the flag and writes stay refused.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"


@pytest.fixture
def home(monkeypatch, tmp_path):
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKSPACES_ROOT", MARKER, "HERMES_DELEGATED_CHILD_OWNER"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    kb.create_board("runners")
    kb.init_db(board="runners")
    return root


def _seed(home):
    with kbc.connect_closing() as conn:
        own = kb.create_task(conn, title="default-board task", assignee="worker")
    with kbc.connect_closing(board="runners") as conn:
        remote = kb.create_task(conn, title="runners-board task", assignee="worker")
        kb.create_task(conn, title="runners-board task 2", assignee="worker")
    return own, remote


def _worker_shell_env(monkeypatch, home, *, task: str | None = None):
    """The env a worker's terminal carries: pins to its own (default) board + the fence marker."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv(MARKER, str(home))
    if task:
        monkeypatch.setenv("HERMES_KANBAN_TASK", task)


def _run(argv, capsys):
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kc.build_parser(parser.add_subparsers(dest="command"))
    rc = kc.kanban_command(parser.parse_args(["kanban", *argv]))
    out = capsys.readouterr()
    return rc, out.out + out.err


@pytest.mark.parametrize("argv", [
    lambda tid: ["--board", "runners", "show", tid],
    lambda tid: ["--board", "runners", "list"],
    lambda tid: ["--board", "runners", "stats"],
])
def test_worker_shell_board_flag_reads_named_board(home, monkeypatch, capsys, argv):
    _, remote = _seed(home)
    _worker_shell_env(monkeypatch, home)
    rc, text = _run(argv(remote), capsys)
    assert rc == 0, text
    assert "no such task" not in text.lower()
    if "show" in argv(remote):
        assert remote in text
    elif "list" in argv(remote):
        assert "runners-board task" in text and "default-board task" not in text
    else:  # stats counts the runners board (2 tasks), not the pinned default (1)
        assert "2" in text


def test_worker_shell_board_flag_still_refuses_writes(home, monkeypatch, capsys):
    _, remote = _seed(home)
    _worker_shell_env(monkeypatch, home)
    rc, text = _run(["--board", "runners", "comment", remote, "must refuse"], capsys)
    assert rc != 0
    assert "kanban_* tools" in text
    monkeypatch.delenv(MARKER)
    with kbc.connect_closing(board="runners") as conn:
        n = conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=?", (remote,)).fetchone()[0]
    assert n == 0


def test_descendant_explicit_board_path_follows_board_inside_fence(home, monkeypatch):
    from agent.delegation_context import kanban_path_is_fenced

    _worker_shell_env(monkeypatch, home)
    path = kb.kanban_db_path(board="runners")
    assert path == kb.board_dir("runners") / "kanban.db"
    assert kanban_path_is_fenced(path)
    # No explicit board: the pin still wins.
    assert kb.kanban_db_path() == home / "kanban.db"


def test_dispatched_worker_and_machine_flows_stay_pinned(home, monkeypatch):
    _worker_shell_env(monkeypatch, home, task="t_worker")
    assert kb.kanban_db_path(board="runners") == home / "kanban.db"
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    with kb.pin_first_board_resolution():
        assert kb.kanban_db_path(board="runners") == home / "kanban.db"


def test_in_process_delegate_child_stays_pinned(home, monkeypatch):
    from agent.delegation_context import delegated_child_context

    _worker_shell_env(monkeypatch, home)
    with delegated_child_context():
        assert kb.kanban_db_path(board="runners") == home / "kanban.db"


@pytest.mark.parametrize("task", [None, "t_worker"])
def test_boards_list_counts_each_boards_own_db(home, monkeypatch, capsys, task):
    _seed(home)
    _worker_shell_env(monkeypatch, home, task=task)
    rc, text = _run(["boards", "list", "--json"], capsys)
    assert rc == 0, text
    rows = {b["slug"]: b for b in json.loads(text)}
    assert rows["default"]["total"] == 1
    assert rows["runners"]["total"] == 2
