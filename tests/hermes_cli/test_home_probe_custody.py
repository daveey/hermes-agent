"""A home move must not relocate shell probes to the receiving gateway."""
from __future__ import annotations

import json

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_wake_check as wake
import pytest
from tests.hermes_cli import test_home_destination_materialize as support

board = support.board


def probe_manifest(*, host=None, ask=False):
    record = support.manifest()
    task = record["rows"][0]["fields"]
    event = record["rows"][2]["fields"]
    payload = {"wake_check": "test -f ~/.hermes/source-only-ready", "wake_at": 9000000, "check_every": 120}
    if host:
        payload["probe_host"] = host
    if ask:
        task["status"] = "blocked"
        task["human_request"] = json.dumps({"resolved_check": payload["wake_check"], "resolved_check_every": 120})
    event.update(kind="blocked" if ask else "scheduled", payload=json.dumps(payload), created_at=1000)
    return record


def test_hostless_park_does_not_register_on_the_new_home(board):
    support.prepare_destination(board)
    record = probe_manifest()
    support.materialize(board, record)
    result = wake.restore_missing_checks(board, now=1001)
    assert result == {"restored": [], "woken": []}
    assert wake.get_state(board, record["task"]) is None


def test_hostless_ask_does_not_register_on_the_new_home(board):
    from hermes_cli import kanban_ask_resolved as ask
    support.prepare_destination(board)
    record = probe_manifest(ask=True)
    support.materialize(board, record)
    result = ask.reconcile(board, now=1001)
    assert result["restored"] == []
    assert wake.get_state(board, record["task"]) is None


def test_explicit_hub_probe_still_registers_on_the_new_home(board):
    support.prepare_destination(board)
    record = probe_manifest(host="hub")
    support.materialize(board, record)
    assert wake.restore_missing_checks(board, now=1001)["restored"] == [record["task"]]
    state = wake.get_state(board, record["task"])
    assert state is not None and state["cmd"] == "test -f ~/.hermes/source-only-ready"


def source_park(conn, *, with_check=True, ask=False):
    from tests.hermes_cli import test_kanban_home_source_stop as stop
    from hermes_cli import kanban_home_transfer as transfer, kanban_ask_resolved as resolved
    tid = stop.support.seed(conn)
    kind = "blocked" if ask else "scheduled"
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (kind,tid))
        kb._append_event(conn, tid, kind, {"wake_check": "test -f ~/.hermes/source-only-ready", "wake_at": 9000000, "check_every": 120})
        if ask:
            conn.execute("UPDATE tasks SET human_request=? WHERE id=?", (json.dumps({"resolved_check": "test -f ~/.hermes/source-only-ready", "resolved_check_every": 120}),tid))
        if with_check:
            if ask:
                resolved.sync_row(conn, tid, now=1000)
            else:
                wake.set_check(conn, tid, "test -f ~/.hermes/source-only-ready", 120, now=1000)
        conn.execute("DELETE FROM kanban_sync_outbox")
        reply = stop.record(conn, tid)
        event = dict(conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind=?", (tid,kind)).fetchone())
        reply["rows"].append({"table": "task_events", "uid": event["uid"], "fields": event})
    return tid, reply, transfer


@pytest.mark.parametrize("ask", [False, True])
def test_preparation_refuses_a_lost_logical_source_probe_before_installing_a_fence(board, ask):
    tid, reply, transfer = source_park(board, with_check=False, ask=ask)
    with kbc.write_txn(board):
        with pytest.raises(ValueError, match="source probe"):
            transfer.prepare(board, tid, source=reply["source"], destination="hub", nonce=reply["nonce"], expected_version=7)
    assert board.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_home_prepared1'").fetchone() is None


@pytest.mark.parametrize("ask", [False, True])
def test_source_copy_survives_a_lost_execution_row_and_restores_there(board, ask):
    from hermes_cli import kanban_worker_node as node, kanban_ask_resolved as resolved
    tid, reply, transfer = source_park(board, ask=ask)
    with kbc.write_txn(board):
        transfer.prepare(board, tid, source=reply["source"], destination="hub", nonce=reply["nonce"], expected_version=7)
        transfer.finalize_source(board, reply)
        board.execute("DELETE FROM task_wake_checks WHERE task_id=?", (tid,))
    assert tid not in node.evict(board)
    assert kb.get_task(board, tid) is not None
    result = resolved.reconcile(board, now=1001) if ask else wake.restore_missing_checks(board, now=1001)
    assert result["restored"] == [tid]


@pytest.mark.parametrize("ask", [False, True])
def test_early_feed_without_destination_receipt_never_guesses_the_new_home(board, ask):
    from hermes_cli import kanban_ask_resolved as resolved
    support.prepare_destination(board)
    record = probe_manifest(ask=ask)
    support.materialize(board, record)
    with kbc.write_txn(board):
        board.execute("DELETE FROM kanban_home_materialized1")
        # A genuinely old hostless event might have no origin before its full
        # authenticated manifest arrives. Do not interpret unknown as local.
        board.execute("UPDATE task_events SET origin_gateway=NULL WHERE uid=?", (record["rows"][2]["uid"],))
    result = resolved.reconcile(board, now=1001) if ask else wake.restore_missing_checks(board, now=1001)
    assert result["restored"] == []
    assert wake.get_state(board, record["task"]) is None


def test_receipt_fallback_only_applies_to_the_exact_historical_event(board):
    from hermes_cli import kanban_home_probe as probe
    support.prepare_destination(board)
    record = probe_manifest()
    support.materialize(board, record)
    tid = record["task"]
    with kbc.write_txn(board):
        board.execute("UPDATE task_events SET origin_gateway=NULL WHERE task_id=?", (tid,))
        old = board.execute("SELECT id FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
        assert probe.host(board, tid, old, None) == "worker-a3"
        kb._append_event(board, tid, "scheduled", {"wake_check": "new probe", "wake_at": 99999999})
        new = board.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
        board.execute("UPDATE task_events SET origin_gateway=NULL WHERE id=?", (new,))
        assert probe.host(board, tid, new, None) == probe.UNKNOWN
        assert probe.host(board, tid, new, "worker-a2") == "worker-a2"
        board.execute("UPDATE task_events SET origin_gateway='hub' WHERE id=?", (new,))
        assert probe.host(board, tid, new, None) == "hub"


def test_prepare_preserves_explicit_foreign_probe_custody_without_registering_it_locally(board):
    tid, reply, transfer = source_park(board, with_check=False)
    with kbc.write_txn(board):
        event = board.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='scheduled'", (tid,)).fetchone()
        payload = json.loads(event[1])
        payload["probe_host"] = "hub"
        board.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload),event[0]))
        board.execute("DELETE FROM kanban_sync_outbox")
        transfer.prepare(board, tid, source=reply["source"], destination="hub", nonce=reply["nonce"], expected_version=7)
    assert wake.get_state(board, tid) is None


def test_ordinary_unmoved_hostless_park_retains_legacy_restoration(board):
    support.prepare_destination(board)
    record = probe_manifest()
    support.materialize(board, record)
    with kbc.write_txn(board):
        board.execute("DELETE FROM kanban_home_generations1 WHERE task_id=?", (record["task"],))
    assert wake.restore_missing_checks(board, now=1001)["restored"] == [record["task"]]
