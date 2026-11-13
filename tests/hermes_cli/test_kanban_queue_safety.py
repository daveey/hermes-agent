"""Review regressions: diagnostics cannot change scheduling or misstate transitions."""
from unittest.mock import Mock
import sqlite3

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
def test_assigned_to_queued_feed_is_not_a_second_transition(conn):
    tid = fixtures.acknowledged(conn)
    assigned = {**reply("reserved"), "state": "assigned", "machine": "worker-1"}
    qr.record_reply(conn, tid, "hub", assigned)
    conn.execute("UPDATE tasks SET runs_on=? WHERE id=?", ('["machine:worker-1"]', tid))
    conn.commit()
    assert qr.record_reply(conn, tid, "hub", reply())
    conn.execute("UPDATE tasks SET runs_on=NULL WHERE id=?", (tid,))
    conn.commit()
    assert qr.latest(conn, tid)["current"]
    assert not qr.record_reply(conn, tid, "hub", reply(at=2000))
    assert len(kb.list_comments(conn, tid)) == 2


@pytest.mark.parametrize("error", [sqlite3.OperationalError("locked"), RuntimeError("comment failed"), OSError("disk")])
def test_record_failure_does_not_skip_successful_reserve_pull(conn, monkeypatch, error):
    fixtures.acknowledged(conn)
    hub = Mock()
    hub.reserve.return_value = reply()
    monkeypatch.setattr(qr, "record_reply", Mock(side_effect=error))
    pull = Mock()
    monkeypatch.setattr(client, "pull", pull)
    assert wp.reevaluate(conn, hub, "default", "hub", cfg={"worker_pools": {"hub": ["worker-1"]}}) == 1
    pull.assert_called_once()


def test_rejection_code_and_class_are_sanitized(conn, monkeypatch):
    tid = fixtures.acknowledged(conn)
    hub = Mock()
    hub.reserve.side_effect = client.HubError(409, '{"error":"worker_pool_disabled","secret":"private"}')
    monkeypatch.setattr(client, "pull", Mock())
    wp.reevaluate(conn, hub, "default", "hub", cfg={"worker_pools": {"hub": ["worker-1"]}})
    value = qr.latest(conn, tid)
    assert value["code"] == "board_refused"
    assert value["rejection_code"] == "worker_pool_disabled"
    assert value["error_class"] == "HubError"
    assert "worker_pool_disabled" in kb.list_comments(conn, tid)[-1].body
    assert "private" not in str(value)


def test_resync_has_no_outage_comment(conn):
    tid = fixtures.acknowledged(conn)
    assert not qr.record_error(conn, tid, "hub", error=client.Resync())
    assert not kb.list_comments(conn, tid)


def test_error_record_failure_preserves_refusal_path(conn, monkeypatch):
    fixtures.acknowledged(conn)
    hub = Mock()
    hub.reserve.side_effect = client.HubError(503, "private")
    monkeypatch.setattr(qr, "record_error", Mock(side_effect=RuntimeError("comment failed")))
    assert wp.reevaluate(conn, hub, "default", "hub", cfg={"worker_pools": {"hub": ["worker-1"]}}) == 0


def test_revision_race_is_not_a_board_outage(conn, monkeypatch):
    tid = fixtures.acknowledged(conn)
    hub = Mock()
    hub.reserve.side_effect = client.HubError(409, '{"error":"revision_conflict"}')
    wp.reevaluate(conn, hub, "default", "hub", cfg={"worker_pools": {"hub": ["worker-1"]}})
    assert qr.latest(conn, tid) is None
    assert not kb.list_comments(conn, tid)
