"""Review v11: an advertising Board's definitive certificate refusal downgrades the park to its ordinary origin."""
from __future__ import annotations

import copy
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_owner_reassessment_lifecycle as lifecycle
from tests.hermes_cli.test_kanban_owner_reassessment_board_compat import BASE_CAP, NEW, _status_ops

worker = lifecycle.worker
REASONS = ["owner_reassessment_binding", "owner_reassessment_time", "owner_reassessment_transition_lost"]


def _board(calls, accepted, script):
    """Advertises the capability. ``script`` answers each certificate-carrying task op in turn:
    a reason string -> definitive rejection, ``"storage_error"`` -> transient, ``None`` -> applied."""
    queue = list(script)

    def ops(board, batch):
        calls.extend(copy.deepcopy(batch))
        out = []
        for op in batch:
            carries = op.get("table") == "tasks" and "owner_reassessment_v1" in op.get("fields", {})
            verdict = queue.pop(0) if carries and queue else None
            if verdict:
                out.append({"client_op_id": op["client_op_id"], "status": "rejected", "reason": verdict, "detail": "x"})
            else:
                accepted.append(copy.deepcopy(op))
                out.append({"client_op_id": op["client_op_id"], "status": "applied", "version": 9})
        return {"results": out}
    return SimpleNamespace(ops=ops, status_origin_capability=lambda board: {**BASE_CAP, "owner_reassessment_v1": 1})


def _run(worker, monkeypatch, script, check, rounds=6):
    def body(cap, ctx, agent, evidence):
        tid = ctx["task_id"]
        with kbc.connect_closing() as conn:
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET sync_version=5 WHERE id=?", (tid,))
                conn.execute("DELETE FROM kanban_sync_outbox")
            assert kb.schedule_task(conn, tid, reason=lifecycle._until(), expected_run_id=ctx["run_id"],
                                    awaiting=lifecycle.PRODUCER,
                                    owner_reassessment=lifecycle._opt(evidence, int(time.time()) + 120), owner_origin=cap)
            park = conn.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='scheduled'", (tid,)).fetchone()[0]
            calls, accepted = [], []
            hub = _board(calls, accepted, script)
            for _ in range(rounds):
                try:
                    client.push(conn, hub, "default", "here", primary=True)
                except OSError:
                    pass
                with kb.write_txn(conn):  # transient backoff elapsed
                    conn.execute("UPDATE kanban_sync_outbox SET retry_after=NULL")
            left = [tuple(r) for r in conn.execute("SELECT state,last_error,status_origin FROM kanban_sync_outbox")]
            return check(conn, park, calls, accepted, left)
    return lifecycle._turn(worker, monkeypatch, body)


@pytest.mark.parametrize("reason", REASONS)
def test_definitive_certificate_refusal_still_delivers_the_park_with_ordinary_origin(worker, monkeypatch, reason):
    def check(conn, park, calls, accepted, left):
        got = _status_ops(accepted)
        assert len(got) == 1, "the advertising Board never received the park's status"
        assert not set(got[0]["fields"]) & set(NEW)
        assert got[0]["status_origin"] == "" != park
        assert not left, left  # nothing rejected is stranded in the outbox
        assert len(_status_ops(calls)) == 2  # the certificate op once, the ordinary park once
        return True
    assert _run(worker, monkeypatch, [reason], check)


@pytest.mark.parametrize("reason", REASONS)
def test_resumed_transient_retry_then_definitive_refusal_still_converges(worker, monkeypatch, reason):
    def check(conn, park, calls, accepted, left):
        got = _status_ops(accepted)
        assert len(got) == 1 and not set(got[0]["fields"]) & set(NEW) and got[0]["status_origin"] == ""
        carrying = [op for op in _status_ops(calls) if "owner_reassessment_v1" in op["fields"]]
        assert len(carrying) == 2 and carrying[0]["client_op_id"] != carrying[1]["client_op_id"]  # fresh id per retry
        assert not left, left
        return True
    assert _run(worker, monkeypatch, ["storage_error", reason], check, rounds=8)


@pytest.mark.parametrize("reason", ["owner_reassessment_other", "owner_reassessment", "forbidden", "conflict"])
def test_unknown_refusal_reasons_are_not_downgraded(worker, monkeypatch, reason):
    def check(conn, park, calls, accepted, left):
        assert not _status_ops(accepted)
        assert len(_status_ops(calls)) == 1 and [r[0] for r in left] == ["rejected"]
        return True
    assert _run(worker, monkeypatch, [reason], check)


def test_unknown_outcome_is_never_downgraded_or_resent(worker, monkeypatch):
    def body(cap, ctx, agent, evidence):
        tid = ctx["task_id"]
        with kbc.connect_closing() as conn:
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET sync_version=5 WHERE id=?", (tid,))
                conn.execute("DELETE FROM kanban_sync_outbox")
            assert kb.schedule_task(conn, tid, reason=lifecycle._until(), expected_run_id=ctx["run_id"],
                                    awaiting=lifecycle.PRODUCER,
                                    owner_reassessment=lifecycle._opt(evidence, int(time.time()) + 120), owner_origin=cap)
            calls, accepted = [], []
            hub = _board(calls, accepted, [])

            def lost(board, batch):
                calls.extend(copy.deepcopy(batch))
                raise OSError("connection reset")
            hub.ops = lost
            with pytest.raises(OSError):
                client.push(conn, hub, "default", "here", primary=True)
            first = _status_ops(calls)
            assert len(first) == 1 and "owner_reassessment_v1" in first[0]["fields"]
            with pytest.raises(OSError):
                client.push(conn, hub, "default", "here", primary=True)
            again = _status_ops(calls)
            # the frozen identical envelope may be replayed, but never a downgraded or re-identified one
            assert all(op == first[0] for op in again)
            assert conn.execute("SELECT count(*) FROM kanban_sync_outbox WHERE table_name='tasks' "
                                "AND wire_json IS NOT NULL AND fields LIKE '%owner_reassessment_v1%'").fetchone()[0] == 1
            assert len({op["client_op_id"] for op in again}) == 1
            return True
    assert lifecycle._turn(worker, monkeypatch, body)
