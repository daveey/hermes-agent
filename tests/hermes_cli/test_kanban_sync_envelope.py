"""First-send envelopes survive lost responses and local row changes. [daveey]"""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support

GUARD_CAP = {"status_origin": 1, "frozen_replay": 1, "hlc": "0000000000000-0000-server"}

@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / "kanban.db")
    client.ensure_identity(conn, "default", "sender")
    yield conn, home
    conn.close()


class LostReply:
    def status_origin_capability(self, board):
        return GUARD_CAP

    def __init__(self):
        self.batches = []

    def ops(self, board, wire):
        self.batches.append(json.dumps(wire, separators=(",", ":")))
        raise OSError("response lost after server applied the batch")


def send(conn, hub):
    with pytest.raises(OSError, match="response lost"):
        client.push(conn, hub, "default", "sender", primary=False)


def test_lost_response_freezes_payload_and_clock_before_network_and_across_restart(board, monkeypatch):
    conn, home = board
    tid = kb.create_task(conn, title="original", assignee="worker")
    hub = LostReply()
    send(conn, hub)
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET title='newer local edit' WHERE id=?", (tid,))
    conn.close()
    reopened = kbc.connect(home / "kanban.db")
    try:
        monkeypatch.setattr(client, "_next_hlc", lambda *_: pytest.fail("retry generated a new clock"))
        # Restrict this batch to the operations actually transmitted before restart.
        old_ids = [op["client_op_id"] for op in json.loads(hub.batches[0])]
        with kbc.write_txn(reopened):
            reopened.execute("DELETE FROM kanban_sync_outbox WHERE op_id NOT IN (" + ",".join("?" for _ in old_ids) + ")", old_ids)
        send(reopened, hub)
        assert hub.batches[1] == hub.batches[0]
    finally:
        reopened.close()


def test_synthesized_parent_identity_and_payload_survive_lost_reply(board):
    conn, _ = board
    tid = kb.create_task(conn, title="parent", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
    kb.add_comment(conn, tid, "author", "child needs parent")
    hub = LostReply()
    send(conn, hub)
    send(conn, hub)
    assert hub.batches[1] == hub.batches[0]
    assert any(op.get("table") == "tasks" for op in json.loads(hub.batches[0]))


def test_release_retries_preserve_entire_envelope(board):
    conn, _ = board
    from hermes_cli import kanban_db_sync as sync
    with kbc.write_txn(conn):
        conn.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) VALUES (?,?,?,?,?,?,?)",
                     (sync.uuid7(), "tasks", "t_release", "release", json.dumps({"run_uid": "run-1", "reason": "failure"}), "", 1))
    hub = LostReply()
    send(conn, hub)
    send(conn, hub)
    assert hub.batches[1] == hub.batches[0]
    assert json.loads(hub.batches[0])[0]["op"] == "release"
