"""First-send clock absorption is distinct from immutable uncertain retries."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_sync_envelope as fixtures

board = fixtures.board


class Ratcheted(fixtures.LostReply):
    clock = "1799999999000-9999-server"

    def status_origin_capability(self, board):
        return {"status_origin": 1, "frozen_replay": 1, "hlc": self.clock}


@pytest.mark.parametrize("parent", [False, True])
def test_first_send_merges_remote_clock_before_freezing_including_parent(board, monkeypatch, parent):
    conn, _ = board
    tid = kb.create_task(conn, title="clock", assignee="worker")
    if parent:
        with kbc.write_txn(conn):
            conn.execute("DELETE FROM kanban_sync_outbox")
        kb.add_comment(conn, tid, "a", "child")
    hub = Ratcheted()
    monkeypatch.setattr(client.time, "time_ns", lambda: 1_790_000_000_000_000_000)
    fixtures.send(conn, hub)
    wires = json.loads(hub.batches[0])
    assert wires and all(op["hlc"][:18] > hub.clock[:18] for op in wires)
    assert all(op["frozen_replay"] == 1 for op in wires)
    first = hub.batches[0]
    hub.clock = "1800000100000-1000-server"
    fixtures.send(conn, hub)
    assert hub.batches[1] == first  # observing newer clocks never restamps a retry


def test_clock_absorption_is_transactional_and_never_regresses(board):
    from hermes_cli.kanban_sync_status_origin import absorb_clock
    conn, _ = board
    initial = conn.execute("SELECT hlc_last FROM kanban_sync_state").fetchone()[0]
    with pytest.raises(RuntimeError, match="rollback"):
        with kbc.write_txn(conn):
            absorb_clock(conn, {"hlc": Ratcheted.clock})
            raise RuntimeError("rollback")
    assert conn.execute("SELECT hlc_last FROM kanban_sync_state").fetchone()[0] == initial
    with kbc.write_txn(conn):
        absorb_clock(conn, {"hlc": Ratcheted.clock})
        absorb_clock(conn, {"hlc": "0000000000000-0000-server"})
    assert conn.execute("SELECT hlc_last FROM kanban_sync_state").fetchone()[0] == Ratcheted.clock


def test_pending_origin_backpressure_is_retryable_not_a_silent_permanent_rejection():
    assert client.transient_rejection({"reason": "not_found", "detail": "status origin pending queue full; deliver lifecycle events before retrying"})
    assert not client.transient_rejection({"reason": "not_found", "detail": "unrelated permanent refusal"})
