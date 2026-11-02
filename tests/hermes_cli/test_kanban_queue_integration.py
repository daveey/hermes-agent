"""Queue diagnostics through dispatch, reserve and operator show entry points."""
import argparse
from contextlib import contextmanager
import json
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_queue_reason as qr
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_worker_reserve_client as fixtures
from tests.hermes_cli.test_kanban_queue_reason import reply

conn = fixtures.conn


@pytest.mark.parametrize("error", [OSError("private transport text"), client.HubError(500, "private response")])
def test_initial_pull_failure_is_durable_and_recovers(conn, monkeypatch, error):
    tid = fixtures.acknowledged(conn)
    foreign = fixtures.acknowledged(conn, created_on="other")
    hub = Mock()
    hub.reserve.return_value = reply("draining", reasons=["draining"])
    monkeypatch.setattr(client, "load_identity", lambda: client.Identity("http://unused", "synthetic", "hub"))
    monkeypatch.setattr(client, "Hub", lambda *a, **kw: hub)
    pull = Mock(side_effect=error)
    monkeypatch.setattr(client, "pull", pull)
    from hermes_cli import kanban_placement
    monkeypatch.setattr(kanban_placement, "config", lambda: {"worker_pools": {"hub": ["worker-1"]}})
    wp.dispatch_tick(conn)
    wp.dispatch_tick(conn)
    assert qr.latest(conn, tid)["code"] == "board_error"
    assert len(kb.list_comments(conn, tid)) == 1
    assert qr.latest(conn, foreign) is None
    assert "private" not in json.dumps(qr.latest(conn, tid))
    hub.reserve.assert_not_called()
    pull.side_effect = None
    wp.dispatch_tick(conn)
    assert qr.latest(conn, tid)["code"] == "draining"
    assert len(kb.list_comments(conn, tid)) == 2
    assert conn.execute("SELECT runs_on FROM tasks WHERE id=?", (tid,)).fetchone()[0] is None
    assert wp.execution_refusal(conn, dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())) == wp.WAITING


@pytest.mark.parametrize("error", [OSError("private transport text"), client.HubError(503, "private response")])
def test_reserve_failure_dedupes_and_recovers(conn, monkeypatch, error):
    tid = fixtures.acknowledged(conn)
    hub = Mock()
    hub.reserve.side_effect = error
    monkeypatch.setattr(client, "pull", Mock())
    cfg = {"worker_pools": {"hub": ["worker-1"]}}
    for _ in range(2):
        wp.reevaluate(conn, hub, "default", "hub", cfg=cfg)
    assert qr.latest(conn, tid)["code"] == "board_error"
    assert len(kb.list_comments(conn, tid)) == 1
    hub.reserve.side_effect = None
    hub.reserve.return_value = {"state": "assigned", "machine": "worker-1"}
    wp.reevaluate(conn, hub, "default", "hub", cfg=cfg)
    assert qr.latest(conn, tid)["state"] == "assigned"
    assert len(kb.list_comments(conn, tid)) == 2
    assert conn.execute("SELECT runs_on FROM tasks WHERE id=?", (tid,)).fetchone()[0] is None


@pytest.mark.parametrize("historical", [False, True])
def test_cli_json_text_tool_and_thread_share_observation(conn, monkeypatch, capsys, historical):
    from hermes_cli import kanban
    from tools import kanban_tools

    tid = fixtures.acknowledged(conn)
    qr.record_reply(conn, tid, "hub", reply("capability_mismatch", reasons=["capability_mismatch"]))
    original = qr.describe(qr.latest(conn, tid))
    assert kb.list_comments(conn, tid)[-1].body == original
    if historical:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        conn.commit()

    @contextmanager
    def closing():
        yield conn

    @contextmanager
    def granted(*args):
        yield kb, conn, "default"

    monkeypatch.setattr(kanban.kbc, "connect_closing", closing)
    monkeypatch.setattr(kanban_tools, "_granted_board", granted)
    args = argparse.Namespace(task_id=tid, json=True)
    assert kanban._cmd_show(args) == 0
    cli = json.loads(capsys.readouterr().out)
    tool = json.loads(kanban_tools._handle_show({"task_id": tid}))
    assert cli["queue_reason"] == tool["queue_reason"] == qr.latest(conn, tid)
    assert cli["queue_reason"]["current"] is not historical
    args.json = False
    assert kanban._cmd_show(args) == 0
    assert qr.describe(cli["queue_reason"]) in capsys.readouterr().out
    assert kb.list_comments(conn, tid)[-1].body == original
