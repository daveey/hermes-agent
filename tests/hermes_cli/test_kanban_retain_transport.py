"""Retain wire bytes are immutable across enqueue, ack and uncertain replay."""
import hashlib
import json

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_failure_release as release
from tests.hermes_cli import test_kanban_failure_transport as support

live = support.live
PAYLOAD = {"version": 1, "reason": "scope_stopped", "summary": "Incomplete scope", "next_step": "Resolve authority",
           "release_condition": "Supported scoped authority", "evidence": ["isolated disposition"]}


def reserve(conn, tid, rid, **changes):
    from hermes_cli import kanban_retain_transport as transport
    with kbc.write_txn(conn):
        return transport.reserve(conn, tid, sync.run_uid(conn, rid), {**PAYLOAD, **changes}, None)


def test_retain_persists_exact_wire_before_io_and_survives_ack(live):
    from hermes_cli import kanban_retain_transport as transport
    from hermes_cli import kanban_sync_wire as wire_ops
    tid, rid = support._running(live)
    row = reserve(live, tid, rid)
    wire = json.loads(row["request_json"])
    assert wire["op"] == "retain" and wire["client_op_id"] == row["op_id"] == row["request_uid"]
    assert wire["event_uid"] == row["event_uid"]
    assert wire["task"] == tid and wire["run_uid"] == sync.run_uid(live, rid)
    assert row["request_sha256"] == hashlib.sha256(row["request_json"].encode()).hexdigest()
    out = sync.outbox(live)
    assert len(out) == 1 and out[0]["op"] == "retain"
    assert wire_ops.translate(out[0], "ignored-new-clock") == wire
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")  # ACK is not canonical receipt confirmation.
    again = reserve(live, tid, rid)
    assert again == row
    assert not release.queued(live, rid) and release.command_owned(live, rid)
    assert transport.command(live, wire["run_uid"])["canonical_receipt_json"] is None
    with pytest.raises(ValueError, match="changed"):
        reserve(live, tid, rid, summary="Different bytes after ACK")
    assert transport.command(live, wire["run_uid"])["request_json"] == row["request_json"]


@pytest.mark.parametrize("release_first", [False, True])
def test_automatic_release_and_retain_first_committed_command_wins(live, release_first):
    tid, rid = support._running(live)
    if release_first:
        with kbc.write_txn(live):
            release.queue(live, tid, rid, outcome="crashed", error="first command")
        row = reserve(live, tid, rid)
        assert row["operation_kind"] == "release" and row["request_json"] is None
    else:
        row = reserve(live, tid, rid)
        with kbc.write_txn(live):
            release.queue(live, tid, rid, outcome="crashed", error="late command")
        assert row["operation_kind"] == "retain"
    assert len(sync.outbox(live)) == 1
    assert sync.outbox(live)[0]["op"] == ("release" if release_first else "retain")
    assert live.execute("SELECT count(*) FROM kanban_failure_commands").fetchone()[0] == 1


def test_retain_reservation_rolls_back_with_outbox(live):
    from hermes_cli import kanban_retain_transport as transport
    tid, rid = support._running(live)
    with pytest.raises(RuntimeError), kbc.write_txn(live):
        transport.reserve(live, tid, sync.run_uid(live, rid), PAYLOAD, None)
        raise RuntimeError("abort")
    assert not sync.outbox(live)
    assert transport.command(live, sync.run_uid(live, rid)) is None


@pytest.mark.parametrize("answer", ["lost_reply", "rejected"])
def test_actual_push_never_enriches_or_regenerates_retain_and_ack_is_not_terminal(live, answer):
    from hermes_cli import kanban_retain_transport as transport
    from hermes_cli import kanban_sync_client as client
    tid, rid = support._running(live)
    owned = reserve(live, tid, rid)
    wire = json.loads(owned["request_json"])
    class Hub:
        probes = 0
        def decision_retain_capability(self, board):
            self.probes += 1
            return {"decision_retain": 1, "frozen_replay": 1, "hlc": "0000000000001-0000-server"}
        def ops(self, board, sent):
            assert sent == [wire]  # No generic envelope frozen_replay/HLC or new op UID.
            if answer == "lost_reply":
                raise OSError("uncertain reply")
            return {"results": [{"client_op_id": owned["op_id"], "status": "rejected", "reason": "storage_error"}]}
    hub = Hub()
    if answer == "lost_reply":
        with pytest.raises(OSError, match="uncertain reply"):
            client.push(live, hub, "default", "fixture", primary=True, max_batches=1)
    else:
        client.push(live, hub, "default", "fixture", primary=True, max_batches=1)
    assert hub.probes == 1
    stored = transport.command(live, wire["run_uid"])
    assert stored["op_id"] == owned["op_id"] and stored["request_json"] == owned["request_json"]
    assert stored["canonical_receipt_json"] is None
    out = list(live.execute("SELECT * FROM kanban_sync_outbox"))
    assert len(out) == 1 and out[0]["op_id"] == owned["op_id"]
    if answer == "lost_reply":
        def acknowledge(board, sent):
            assert sent == [wire]
            return {"results": [{"client_op_id": owned["op_id"], "status": "applied"}]}
        hub.ops = acknowledge
        client.push(live, hub, "default", "fixture", primary=True, max_batches=1)
        assert hub.probes == 2 and not sync.outbox(live)
        assert transport.command(live, wire["run_uid"])["canonical_receipt_json"] is None


def test_actual_push_flushes_prior_writes_then_sends_retain_alone(live):
    from hermes_cli import kanban_db as kb, kanban_sync_client as client
    tid, rid = support._running(live)
    kb.add_comment(live, tid, "fixture", "administrative evidence")
    owned = reserve(live, tid, rid)
    wire = json.loads(owned["request_json"])
    class Hub:
        calls = []
        def decision_retain_capability(self, board):
            return {"decision_retain": 1, "frozen_replay": 1, "hlc": "0000000000001-0000-server"}
        def ops(self, board, sent):
            self.calls.append(sent)
            if any(op["op"] == "retain" for op in sent):
                assert sent == [wire]
            return {"results": [{"client_op_id": op["client_op_id"], "status": "applied", "version": 1} for op in sent]}
    hub = Hub()
    client.push(live, hub, "default", "fixture", primary=True, max_batches=3)
    assert len(hub.calls) == 2 and hub.calls[0][0]["table"] == "task_comments" and hub.calls[1] == [wire]
    assert not sync.outbox(live)


@pytest.mark.parametrize("bad", [{}, {"decision_retain": True, "frozen_replay": 1, "hlc": "0000000000001-0000-server"},
    {"decision_retain": 1, "frozen_replay": 1, "hlc": "0000000000001-0000-client"}])
def test_capability_is_serving_protocol_not_truthy_or_client_clock(bad):
    from hermes_cli import kanban_retain_transport as transport
    class Hub:
        def decision_retain_capability(self, board):
            return bad
    with pytest.raises(ValueError, match="deploy"):
        transport.capability(Hub(), "default")
