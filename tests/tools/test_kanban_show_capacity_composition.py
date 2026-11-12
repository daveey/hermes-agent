"""Capacity diagnostics compose only with the live-card read authority."""
import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_queue_position as queue
from tests.tools import test_kanban_show_replica as replica_tests
from tools import kanban_tools as kt

boards = replica_tests.boards


def test_replica_does_not_collect_local_capacity(boards, monkeypatch):
    tid, shadow = boards
    before = shadow.read_bytes()

    def local_capacity_forbidden(*args, **kwargs):
        raise AssertionError("replica card cannot describe this gateway's capacity")

    monkeypatch.setattr(queue, "detail_fields", local_capacity_forbidden)
    out = json.loads(kt._handle_show({"task_id": tid}))
    assert out["source"] == "replica"
    assert out["owner"] == "owner-gateway"
    assert out["task"]["id"] == tid
    assert not {"capacity_evidence", "execution_observations", "capacity_note", "queue", "queue_hint"} & out.keys()
    assert out["undelivered_comments"] == []
    assert shadow.read_bytes() == before
    with kbc.connect() as live:
        assert kb.get_task(live, tid) is None


def test_live_capacity_uses_live_connection_and_requested_board(boards, monkeypatch):
    tid, _ = boards
    monkeypatch.setattr(replica_tests.sync, "new_task_id", lambda conn: tid)
    with kbc.connect() as live:
        assert kb.create_task(live, title="Live capacity subject", assignee="default") == tid
    calls = []

    def detail_fields(conn, task, board):
        calls.append((task.title, board))
        assert kb.get_task(conn, tid).title == "Live capacity subject"
        return {"capacity_evidence": {"freshness": "unknown"}, "execution_observations": [],
                "capacity_note": "fixture unknown"}

    monkeypatch.setattr(queue, "detail_fields", detail_fields)
    out = json.loads(kt._handle_show({"task_id": tid, "board": "default"}))
    assert out["source"] == "live"
    assert out["capacity_evidence"] == {"freshness": "unknown"}
    assert out["execution_observations"] == []
    assert out["capacity_note"] == "fixture unknown"
    assert calls == [("Live capacity subject", "default")]
    assert "owner" not in out
