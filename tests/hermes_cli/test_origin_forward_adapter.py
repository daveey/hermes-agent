"""Execute registered forwarding argv with real session and accepted-ledger stores."""
import argparse
import io
import json

import pytest

from hermes_cli import kanban, kanban_sync_client
from hermes_cli.kanban_parser import build_parser
from tests.hermes_cli import test_kanban_origin_adapter as helpers

world = helpers.world


def invoke(world, monkeypatch, capsys, envelope):
    parser = build_parser(argparse.ArgumentParser().add_subparsers())
    args = parser.parse_args(["origin-forward", "--request-file", "-", "--json"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(envelope)))
    monkeypatch.setattr(kanban.kb, "init_db", lambda: pytest.fail("must not initialize a board"))
    assert kanban.kanban_command(args) == 0
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def envelope(world, monkeypatch, capsys):
    assert helpers.invoke(world, monkeypatch, capsys)["status"] == "accepted"
    binding = world[3]["args"]["binding"]
    monkeypatch.setattr(kanban_sync_client, "load_identity", lambda: None)
    monkeypatch.setattr(kanban_sync_client.Hub, "snapshot_page", lambda *a, **k: {
        "table": "leases", "rows": [{"task_id": "t_one", "holder": "worker-7", "run_uid": "run-1",
                                    "expires": 9999999999999}], "more": False})
    return {"id": "placement:worker-7:test", "from": "gw:worker-7", "to_gw": "origin",
            "coll": "sessions", "key": "default/s_old", "action": "origin-forward",
            "args": {"origin": binding["origin"], "creator": binding["owner"], "run_uid": "run-1",
                     "destination": {"gateway": "owner", "profile": "work", "board": "build"},
                     "create": {"title": "child", "assignee": "work", "session_id": "creator-1"}}}


def test_actual_parser_adapter_persists_then_replays_offline(world, monkeypatch, capsys, envelope):
    first = invoke(world, monkeypatch, capsys, envelope)
    assert first["status"] == "prepared" and first["handoff_complete"] is False
    assert first["outbound"]["args"]["origin"]["gateway"] == "origin"
    assert "from" not in first["outbound"]
    assert (world[0] / "kanban/origin-forward.db").exists()
    monkeypatch.setattr(kanban_sync_client, "load_identity", lambda: pytest.fail("replay must not read network identity"))
    assert invoke(world, monkeypatch, capsys, envelope) == first


@pytest.mark.parametrize("mode", ["offline", "malformed", "missing", "wrong-holder"])
def test_authority_failure_no_outbound(world, monkeypatch, capsys, envelope, mode):
    def snapshot(*args, **kwargs):
        if mode == "offline":
            raise OSError("offline")
        if mode == "malformed":
            return {}
        return {"table": "leases", "rows": [] if mode == "missing" else [
            {"task_id": "t_one", "holder": "worker-8", "run_uid": "run-1", "expires": 9999999999999}], "more": False}
    monkeypatch.setattr(kanban_sync_client.Hub, "snapshot_page", snapshot)
    result = invoke(world, monkeypatch, capsys, envelope)
    assert result["status"] == ("pending" if mode in {"offline", "malformed"} else "rejected")
    assert result["handoff_complete"] is False and "outbound" not in result


def test_delegated_fence_uses_forward_store(world, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(world[0].parent / "other-board"))
    monkeypatch.setattr("agent.delegation_context.kanban_path_is_fenced",
                        lambda path: path == world[0] / "kanban/origin-forward.db")
    args = argparse.Namespace(kanban_action="origin-forward", request_file="-")
    assert kanban.kanban_command(args) != 0
    assert not (world[0] / "kanban/origin-forward.db").exists()
