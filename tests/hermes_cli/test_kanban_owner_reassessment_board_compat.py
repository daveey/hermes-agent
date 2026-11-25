"""An opt-in park reaches a Board by what it advertises; the stored capture binding is never lost."""
from __future__ import annotations

import copy
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_owner_reassessment_lifecycle as lifecycle

worker = lifecycle.worker
NEW = ("owner_reassessment_v1", "owner_reassessment_fallback_status_origin")
BASE_CAP = {"status_origin": 1, "frozen_replay": 1, "hlc": "1795610000000-0001-server"}


def _board(calls, accepted, *, advertise, refuse):
    """``advertise``: capability key; ``refuse``: answer the new fields with hub main's exact rejection."""
    def ops(board, batch):
        calls.extend(copy.deepcopy(batch))
        out = []
        for op in batch:
            bad = [k for k in op.get("fields", {}) if k in NEW]
            if op.get("table") == "tasks" and bad and refuse:
                out.append({"client_op_id": op["client_op_id"], "status": "rejected", "reason": "forbidden_field",
                            "detail": "not writable on tasks: " + ", ".join(sorted(bad))})
            else:
                accepted.append(copy.deepcopy(op))
                out.append({"client_op_id": op["client_op_id"], "status": "applied", "version": 9})
        return {"results": out}
    cap = {**BASE_CAP, **({"owner_reassessment_v1": 1} if advertise else {})}
    return SimpleNamespace(ops=ops, status_origin_capability=lambda board: dict(cap), cap=cap)


def _scenario(worker, monkeypatch, check, *, advertise, refuse, rounds=4):
    def body(cap, ctx, agent, evidence):
        tid = ctx["task_id"]
        with kbc.connect_closing() as conn:
            with kb.write_txn(conn):  # the Board already holds this running card
                conn.execute("UPDATE tasks SET sync_version=5 WHERE id=?", (tid,))
                conn.execute("DELETE FROM kanban_sync_outbox")
            assert kb.schedule_task(conn, tid, reason=lifecycle._until(), expected_run_id=ctx["run_id"],
                                    awaiting=lifecycle.PRODUCER,
                                    owner_reassessment=lifecycle._opt(evidence, int(time.time()) + 120), owner_origin=cap)
            park = conn.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='scheduled'", (tid,)).fetchone()[0]
            stored = conn.execute("SELECT fields,status_origin FROM kanban_sync_outbox WHERE table_name='tasks'").fetchall()
            calls, accepted = [], []
            hub = _board(calls, accepted, advertise=advertise, refuse=refuse)
            for _ in range(rounds):
                client.push(conn, hub, "default", "here", primary=True)
            left = [tuple(r) for r in conn.execute(
                "SELECT state,last_error,status_origin FROM kanban_sync_outbox WHERE table_name='tasks'")]
            return check(conn, hub, park, stored, calls, accepted, left)
    return lifecycle._turn(worker, monkeypatch, body)


def _status_ops(ops):
    return [op for op in ops if op.get("table") == "tasks" and op.get("fields", {}).get("status") == "scheduled"]


@pytest.mark.parametrize("advertise", [False, True])
def test_board_refusing_both_fields_still_receives_park_status_with_ordinary_origin(worker, monkeypatch, advertise):
    """advertise=False: wire gate strips at serialization. advertise=True but refused (rolling deploy):
    the definitive downgrade restores the saved ordinary origin and drops both fields together."""
    def check(conn, hub, park, stored, calls, accepted, left):
        got = _status_ops(accepted)
        assert got, "the old Board never received the park's status"
        assert all(not (set(op["fields"]) & set(NEW)) for op in got)
        assert all(op.get("status_origin") != park for op in got)
        assert [op["status_origin"] for op in got] == [""]  # the saved ordinary (no terminal/reopen event)
        assert not [r for r in left if r[0] == "rejected"]
        assert not left
        # the captured local binding was stored on the exact op before any wire existed
        assert any("owner_reassessment_v1" in f and so == park for f, so in stored)
        return True
    assert _scenario(worker, monkeypatch, check, advertise=advertise, refuse=True)


def test_accepting_board_gets_receipt_on_the_exact_op(worker, monkeypatch):
    def check(conn, hub, park, stored, calls, accepted, left):
        got = _status_ops(accepted)
        assert len(got) == 1 and set(NEW) <= set(got[0]["fields"])
        assert got[0]["status_origin"] == park
        assert got[0]["fields"]["owner_reassessment_v1"]["park_event"]["uid"] == park
        assert got[0]["fields"]["owner_reassessment_fallback_status_origin"] == ""
        assert not left
        return True
    assert _scenario(worker, monkeypatch, check, advertise=True, refuse=False)


def test_frozen_receipt_envelope_is_not_rewritten_or_resent_after_capability_is_lost(worker, monkeypatch):
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
            lost = _board(calls, accepted, advertise=True, refuse=False)

            def unknown(board, batch):  # the answer is lost after the Board saw it
                calls.extend(copy.deepcopy(batch))
                raise OSError("connection reset")
            lost.ops = unknown
            with pytest.raises(OSError):
                client.push(conn, lost, "default", "here", primary=True)
            sent = _status_ops(calls)
            assert len(sent) == 1 and set(NEW) <= set(sent[0]["fields"])
            old = _board(calls, accepted, advertise=False, refuse=False)
            before = len(calls)
            with pytest.raises(OSError):
                client.push(conn, old, "default", "here", primary=True)
            assert len(calls) == before, "an unknown-outcome envelope must not be rebuilt and resent"
            assert conn.execute("SELECT wire_json IS NOT NULL FROM kanban_sync_outbox WHERE table_name='tasks'").fetchone()[0]
            return True
    assert lifecycle._turn(worker, monkeypatch, body)
