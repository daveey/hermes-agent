"""Reviewer probes for PR 3913 @66bf8e1894 (not part of the PR). Every hub here advertises the capability,
so the frozen (activated) path runs, unlike a bare SimpleNamespace(ops=...) hub."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support

CAP = {"status_origin": 1, "frozen_replay": 1, "hlc": "0000000000000-0000-server"}


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / "kanban.db")
    client.ensure_identity(conn, "default", "sender")
    yield conn, home
    conn.close()


class Hub:
    def __init__(self, ops, cap=True):
        self.ops = ops
        if cap:
            self.status_origin_capability = lambda board: CAP


@pytest.mark.parametrize("cap", [True, False])
@pytest.mark.parametrize("reason", ["live_claim", "invalid"])
def test_p1_parent_recovers_after_permanent_refusal_lifts(board, monkeypatch, reason, cap):
    conn, _ = board
    tid = kb.create_task(conn, title="parent", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
    kb.add_comment(conn, tid, "author", "child")
    receipts, known, refusing, parent_ids, sizes = {}, set(), [True], [], []

    def ops(_board, wire):
        results = []
        sizes.append(len(wire))
        for op in wire:
            cid = op["client_op_id"]
            if cid not in receipts:
                if op["table"] == "tasks":
                    if refusing[0]:
                        receipts[cid] = {"status": "rejected", "reason": reason}
                    else:
                        known.add(op["uid"])
                        receipts[cid] = {"status": "applied", "version": 1}
                elif (op.get("fields") or {}).get("task_id") in known:
                    receipts[cid] = {"status": "applied", "version": 1}
                else:
                    receipts[cid] = {"status": "rejected", "reason": "depends_on_rejected"}
            if op["table"] == "tasks":
                parent_ids.append(cid)
            results.append({"client_op_id": cid, **receipts[cid]})
        return {"results": results}

    now = [client.time.time()]
    monkeypatch.setattr(client.time, "time", lambda: now[0])
    for i in range(10):
        if i == 3:
            refusing[0] = False
        client.push(conn, Hub(ops, cap), "default", "sender", primary=False, max_batches=1)
        now[0] += 400
    print("P1", reason, "cap", cap, "distinct parent ids:", len(set(parent_ids)), "sends:", len(parent_ids),
          "acked:", tid in known, "batch sizes:", sizes,
          "task rows:", [tuple(r) for r in conn.execute("SELECT state,attempts FROM kanban_sync_outbox WHERE table_name='tasks'")],
          "envelopes:", conn.execute("SELECT COUNT(*) FROM kanban_sync_parent_envelopes").fetchone()[0],
          "sync_rejected events:", conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='sync_rejected'").fetchone()[0])
    assert tid in known


@pytest.mark.parametrize("cap", [True, False])
def test_p2_children_during_parent_backoff(board, monkeypatch, cap):
    conn, _ = board
    tid = kb.create_task(conn, title="parent v1", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
    kb.add_comment(conn, tid, "author", "child")
    down, sent = [True], []

    def ops(_board, wire):
        results = []
        for op in wire:
            if down[0]:
                reason = "storage_error" if op["table"] == "tasks" else "depends_on_rejected"
                results.append({"client_op_id": op["client_op_id"], "status": "rejected", "reason": reason})
            else:
                sent.append(op)
                results.append({"client_op_id": op["client_op_id"], "status": "applied", "version": len(sent)})
        return {"results": results}

    hub = Hub(ops, cap)
    now = [client.time.time()]
    monkeypatch.setattr(client.time, "time", lambda: now[0])
    peak = 0
    for i in range(6):
        client.push(conn, hub, "default", "sender", primary=False, max_batches=1)
        now[0] += 400
        kb.add_comment(conn, tid, "author", f"later child {i}")
        client.push(conn, hub, "default", "sender", primary=False, max_batches=1)
        kb.add_comment(conn, tid, "author", f"in-backoff child {i}")
        client.push(conn, hub, "default", "sender", primary=False, max_batches=1)
        peak = max(peak, conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE table_name='tasks'").fetchone()[0])
    rows = [tuple(r) for r in conn.execute("SELECT state, attempts FROM kanban_sync_outbox WHERE table_name='tasks'")]
    down[0] = False
    for _ in range(6):
        now[0] += 400
        client.push(conn, hub, "default", "sender", primary=False, max_batches=1)
    task_ops = [op for op in sent if op["table"] == "tasks"]
    left = [tuple(r) for r in conn.execute("SELECT table_name,state,attempts FROM kanban_sync_outbox")]
    print("P2 cap", cap, "peak task rows:", peak, "rows while down:", rows, "task upserts delivered:", len(task_ops),
          "leftover:", left)
    assert peak <= 1 and len(task_ops) == 1 and not left


class Lost:
    def __init__(self):
        self.batches = []
        self.status_origin_capability = lambda board: CAP

    def ops(self, board, wire):
        self.batches.append(json.loads(json.dumps(wire)))
        raise OSError("lost")


@pytest.mark.parametrize("captured_in_txn", [True, False])
def test_p3_frozen_wake_after_completion_arrives(board, captured_in_txn):
    conn, _ = board
    tid = kb.create_task(conn, title="acked", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET sync_version=1 WHERE id=?", (tid,))
        conn.execute("DELETE FROM kanban_sync_outbox")
    if captured_in_txn:
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
    else:
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        conn.commit()
    hub = Lost()
    with pytest.raises(OSError):
        client.push(conn, hub, "default", "sender", primary=False)
    first_ids = [op["client_op_id"] for op in hub.batches[0]]
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox WHERE op_id NOT IN (%s)" % ",".join("?" * len(first_ids)), first_ids)  # noqa: UP031 -- unchanged independent probe
    with kbc.write_txn(conn):
        kb._append_event(conn, tid, "completed", {})
        conn.execute("DELETE FROM kanban_sync_outbox WHERE op_id NOT IN (%s)" % ",".join("?" * len(first_ids)), first_ids)  # noqa: UP031 -- unchanged independent probe
    with pytest.raises(OSError):
        client.push(conn, hub, "default", "sender", primary=False)
    show = lambda b: [(op["fields"].get("status"), op.get("status_origin", "<none>"), op.get("frozen_replay"), op["hlc"])
                      for op in b if op["table"] == "tasks"]
    print("P3 in_txn", captured_in_txn, "first:", show(hub.batches[0]), "second:", show(hub.batches[1]),
          "identical:", hub.batches[0] == hub.batches[1])
    second = [op for op in hub.batches[1] if op["table"] == "tasks"]
    for op in second:
        if "status" in op["fields"]:
            # a status that still travels after the completion arrived must be origin-bound and marked
            assert op.get("frozen_replay") == 1 and "status_origin" in op, op


def _fake_board(known, log):
    def ops(_board, wire):
        results = []
        for op in wire:
            cid = op["client_op_id"]
            if op["table"] == "tasks":
                known.add(op["uid"])
                res = {"status": "applied", "version": 1}
            elif (op.get("fields") or {}).get("task_id") in known:
                res = {"status": "applied", "version": 1}
            else:
                res = {"status": "rejected", "reason": "not_found", "detail": f"no task {(op.get('fields') or {}).get('task_id')}"}
            log.append((op["table"], res["status"]))
            results.append({"client_op_id": cid, **res})
        return {"results": results}
    return ops


@pytest.mark.parametrize("cap", [True, False])
def test_p4_child_ahead_of_its_cards_row_beyond_the_batch(board, monkeypatch, cap):
    """A child queued before its unacknowledged card's own tasks row, with that row beyond this batch's tail."""
    conn, _ = board
    filler = kb.create_task(conn, title="filler", assignee="worker")
    tid = kb.create_task(conn, title="late card", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET sync_version=1 WHERE id=?", (filler,))
        conn.execute("DELETE FROM kanban_sync_outbox")
    kb.add_comment(conn, tid, "author", "child ahead of its card")
    for i in range(client.PUSH_BATCH + 5):
        kb.add_comment(conn, filler, "author", f"filler {i}")
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET title='late card edited' WHERE id=?", (tid,))
    order = [r[0] for r in conn.execute("SELECT table_name FROM kanban_sync_outbox ORDER BY created_at,rowid")]
    assert order[-1] == "tasks" and order.index("tasks") >= client.PUSH_BATCH, (order.index("tasks"), len(order))
    known, log = {filler}, []
    hub = Hub(_fake_board(known, log), cap)
    now = [client.time.time()]
    monkeypatch.setattr(client.time, "time", lambda: now[0])
    rounds = []
    for _ in range(6):
        res = client.push(conn, hub, "default", "sender", primary=False)
        rounds.append((res.sent, res.applied, res.deferred, res.rejected))
        if not conn.execute("SELECT 1 FROM kanban_sync_outbox").fetchone():
            break
        now[0] += 1
    refused = sum(1 for _t, s in log if s == "rejected")
    left = [tuple(r) for r in conn.execute("SELECT table_name,state,attempts,last_error FROM kanban_sync_outbox")]
    print("P4 cap", cap, "rounds (sent,applied,deferred,rejected):", rounds, "Board refusals:", refused, "leftover:", left)
    assert refused == 0, "the child reached the Board ahead of its card"
