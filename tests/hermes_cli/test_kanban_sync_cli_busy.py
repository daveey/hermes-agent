"""Busy native detach is an actionable CLI refusal, without changing custody."""
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_sync as sync, kanban_sync_cli as cli
from hermes_cli.kanban_sync_guard import board_sync_lock
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def synced_conn(kanban_home):
    yield from support.synced_conn(kanban_home)


def test_busy_detach_cli_preserves_custody_and_retry_succeeds(synced_conn, monkeypatch, capsys):
    conn = synced_conn
    tid = kb.create_task(conn, title="synthetic custody", assignee="worker")
    assert kb.claim_task(conn, tid)
    cli.ksc.ensure_identity(conn, "default", "fixture-host")
    sync.set_mode(conn, "primary")
    path = Path(sync._db_key(conn))
    monkeypatch.setattr(cli, "_path", lambda slug: path)
    monkeypatch.setattr(cli.ksc, "load_identity", lambda: SimpleNamespace(
        gateway="fixture-host", url="https://board.example.invalid"))
    args = Namespace(sync_action="attach", board="default", mode="off", json=False)
    before = [tuple(row) for row in conn.execute("SELECT * FROM task_runs")]
    with board_sync_lock(path):
        assert cli.cmd_sync(args) == 2
        output = capsys.readouterr()
        assert "board sync round is busy; retry after it finishes" in output.out
        assert "Traceback" not in output.out + output.err
        assert sync.board_mode(conn) == "primary"
        assert kb.get_task(conn, tid).status == "running"
        assert [tuple(row) for row in conn.execute("SELECT * FROM task_runs")] == before
    assert cli.cmd_sync(args) == 0
    assert "mode off" in capsys.readouterr().out
    assert sync.board_mode(conn) == "off"
    assert kb.get_task(conn, tid).status == "running"
    assert [tuple(row) for row in conn.execute("SELECT * FROM task_runs")] == before


def test_unexpected_cli_error_is_not_hidden(monkeypatch):
    def broken(args):
        raise RuntimeError("unexpected defect")

    monkeypatch.setitem(cli._ACTIONS, "attach", broken)
    with pytest.raises(RuntimeError, match="unexpected defect"):
        cli.cmd_sync(Namespace(sync_action="attach"))
