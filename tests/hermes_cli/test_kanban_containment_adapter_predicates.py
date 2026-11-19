"""Public refusal envelopes never report an unauthorized request as success."""
from __future__ import annotations

import argparse
import json
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_containment_cli as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as connections
from tests.hermes_cli import test_kanban_containment_service as fixtures
from tools import kanban_containment_tools as tools
from tools import kanban_tools

board = fixtures.board


@pytest.mark.parametrize("release", [False, True])
def test_cli_and_tool_unadmitted_requests_have_explicit_false_envelopes(board, monkeypatch, capsys, release):
    relay, authority, _ = fixtures.arguments(board, monkeypatch)
    monkeypatch.delenv("HERMES_KANBAN_TASK")

    @contextmanager
    def opened(*_a, **_k):
        yield kb, board

    @contextmanager
    def connection(*_a, **_k):
        yield board

    monkeypatch.setattr(kanban_tools, "_board", opened)
    monkeypatch.setattr(connections, "connect_closing", connection)
    args = {"task_id": relay, "authority": authority, "summary": "Fixture", "episode": "invalid", "kind": "authority"}
    handler = tools.handle_resume if release else tools.handle_contain
    result = json.loads(handler(args))
    assert result["ok"] is False and result["code"] == "containment_refused"
    namespace = argparse.Namespace(**args, board=None, producer=None,
                                   kanban_action="resume-contained" if release else "contain")
    assert cli.command(namespace) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False and result["code"] == "containment_refused"
    row = kb.get_task(board, relay)
    assert row is not None and row.status == "running"
    assert not board.execute("SELECT 1 FROM task_events WHERE kind IN ('contained','containment_released')").fetchone()
