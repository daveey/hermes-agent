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
from tests.hermes_cli import test_kanban_queue_reason_hold as hold

no_comment_hold = hold.no_comment_hold
conn = fixtures.conn


@pytest.mark.usefixtures("no_comment_hold")
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


@pytest.mark.usefixtures("no_comment_hold")
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


ORIGIN_REASON = "no pool worker can run it: missing capability:file, capability:git"


def origin_reply(reason=ORIGIN_REASON):
    return {"state": "origin", "machine": "hub", "reservation_id": None, "expires_at": None, "reason": reason,
            "observation": {"version": 1, "code": "no_pool_worker", "observed_at_ms": 1000, "candidates": [
                {"machine": "worker-1", "reasons": ["missing_labels"],
                 "missing_labels": ["capability:file", "capability:git"]}]}}


def _apply_board_repin(conn, tid):
    """What the authoritative feed carries after the Board's structural CAS repin."""
    conn.execute("UPDATE kanban_sync_state SET applying=1")
    conn.execute("UPDATE tasks SET placement_policy=NULL, runs_on=? WHERE id=?", (json.dumps(["machine:hub"]), tid))
    conn.execute("UPDATE kanban_sync_state SET applying=0")
    conn.commit()


def test_legacy_board_origin_return_records_reason_and_unfences_origin(conn, monkeypatch, capsys):
    """LEGACY Board (pre-overflow, hermes-hub#69) state=origin reply: still recorded and shown during a rolling deploy.
    Overflow Boards no longer send it (review ruling 9): see test_no_eligible_worker_queues_and_keeps_fence."""
    from hermes_cli import kanban

    tid = fixtures.acknowledged(conn, requires=json.dumps(["terminal", "file", "git"]))
    hub = Mock()
    hub.reserve.return_value = origin_reply()
    monkeypatch.setattr(client, "pull", lambda *a, **kw: _apply_board_repin(conn, tid))
    assert wp.reevaluate(conn, hub, "default", "hub", cfg={"worker_pools": {"hub": ["worker-1"]}}) == 1
    value = qr.latest(conn, tid)
    assert value["code"] == "no_pool_worker" and value["state"] == "origin" and value["machine"] == "hub"
    assert value["reason"] == ORIGIN_REASON
    assert value["current"] is True
    assert value["candidates"] == [{"machine": "worker-1", "reasons": ["missing_labels"],
                                    "missing_labels": ["capability:file", "capability:git"]}]
    assert ORIGIN_REASON in kb.list_comments(conn, tid)[-1].body
    row = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert wp.execution_refusal(conn, row) is None
    assert tid not in wp.local_refusals(conn, "ready")

    @contextmanager
    def closing():
        yield conn

    monkeypatch.setattr(kanban.kbc, "connect_closing", closing)
    assert kanban._cmd_show(argparse.Namespace(task_id=tid, json=False)) == 0
    assert ORIGIN_REASON in capsys.readouterr().out
    # The reason stays current through the origin's own run; only a later re-placement makes it historical.
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    conn.commit()
    assert qr.latest(conn, tid)["current"] is True
    conn.execute("UPDATE tasks SET runs_on=? WHERE id=?", (json.dumps(["machine:worker-1"]), tid))
    conn.commit()
    assert "[historical]" in qr.describe(qr.latest(conn, tid))


def test_origin_reason_text_is_allowlisted(conn):
    tid = fixtures.acknowledged(conn)
    assert qr.record_reply(conn, tid, "hub", origin_reply(reason="no pool worker can run it: missing x; injected text"))
    value = qr.latest(conn, tid)
    assert value["code"] == "no_pool_worker" and "reason" not in value
    assert "injected" not in kb.list_comments(conn, tid)[-1].body


def test_origin_reason_is_current_before_feed_and_survives_pull_error(conn):
    """Between the reserve reply and the feed applying the repin, the return is decided: it is shown as current,
    and a failed feed read in that window must not supersede it with a board_error."""
    tid = fixtures.acknowledged(conn, requires=json.dumps(["terminal", "file", "git"]))
    assert qr.record_reply(conn, tid, "hub", origin_reply())
    pending = qr.latest(conn, tid)
    assert pending["state"] == "origin" and pending["current"] is True
    assert "[historical]" not in qr.describe(pending)
    qr.record_pull_error(conn, "hub", status=503)
    assert qr.latest(conn, tid)["state"] == "origin"
    _apply_board_repin(conn, tid)
    applied = qr.latest(conn, tid)
    assert applied["state"] == "origin" and applied["current"] is True and applied["reason"] == ORIGIN_REASON


def test_no_eligible_worker_queues_and_keeps_fence(conn, monkeypatch):
    """Overflow Board (ruling 9): no automatic origin return. A card no pool worker can run is queued with
    `no_eligible_worker`; its fence stays and the origin may NOT run it until an explicit owner repin."""
    tid = fixtures.acknowledged(conn, requires=json.dumps(["terminal", "file", "git"]))
    hub = Mock()
    hub.reserve.return_value = {"state": "queued", "machine": None, "reservation_id": None, "expires_at": None,
                                "reason": "waiting_for_worker_capacity",
                                "observation": {"version": 1, "code": "no_eligible_worker", "observed_at_ms": 1000,
                                                "candidates": [{"machine": "worker-1", "reasons": ["missing_labels"],
                                                                "missing_labels": ["capability:file"]}]}}
    monkeypatch.setattr(client, "pull", lambda *a, **kw: None)
    assert wp.reevaluate(conn, hub, "default", "hub", cfg={"worker_pools": {"hub": ["worker-1"]}}) == 1
    value = qr.latest(conn, tid)
    assert value["code"] == "no_eligible_worker" and value["state"] == "queued"
    row = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert row["placement_policy"] == "worker-first-v1"
    assert wp.execution_refusal(conn, row) == wp.WAITING
    assert tid in wp.local_refusals(conn, "ready")
