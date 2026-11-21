"""Reviewer probe 5 for PR 3913 @66bf8e1894 (not part of the PR).

The fake Board follows hermes-hub fbbf27e worker/src/board.ts applyOps: ops apply in order, a child of an unknown
card is refused not_found "no task <id>" (rows.ts:231), and every later op on a card that had a refusal in the same
request is refused depends_on_rejected (board.ts:344). Answers are remembered by client_op_id."""
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
    yield conn
    conn.close()


class Board:
    def __init__(self, cap, known=()):
        self.known, self.receipts, self.storage_down, self.log = set(known), {}, False, []
        if cap:
            self.status_origin_capability = lambda board: CAP

    def ops(self, _board, wire):
        results, rejected_cards = [], set()
        for op in wire:
            cid = op["client_op_id"]
            card = op["uid"] if op["table"] == "tasks" else (op.get("fields") or {}).get("task_id")
            if cid in self.receipts:
                res = self.receipts[cid]
            elif card in rejected_cards:
                res = {"status": "rejected", "reason": "depends_on_rejected"}
            elif op["table"] == "tasks":
                if self.storage_down:
                    res = {"status": "rejected", "reason": "storage_error"}
                else:
                    self.known.add(card)
                    res = {"status": "applied", "version": 1}
            elif card is None or card in self.known:
                res = {"status": "applied", "version": 1}
            else:
                res = {"status": "rejected", "reason": "not_found", "detail": f"no task {card}"}
            self.receipts[cid] = res
            if res["status"] == "rejected" and card:
                rejected_cards.add(card)
            self.log.append((op["table"], res["status"], res.get("reason")))
            results.append({"client_op_id": cid, **res})
        return {"results": results}


@pytest.mark.parametrize("cap", [True, False])
def test_p5_one_transient_parent_refusal_then_healthy_board(board, monkeypatch, cap):
    """An unacknowledged card with only child rows queued; the Board has ONE storage hiccup, then is healthy."""
    conn = board
    tid = kb.create_task(conn, title="card", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
    kb.add_comment(conn, tid, "author", "child")
    hub = Board(cap)
    now = [client.time.time()]
    monkeypatch.setattr(client.time, "time", lambda: now[0])
    hub.storage_down = True
    client.push(conn, hub, "default", "sender", primary=False, max_batches=1)
    hub.storage_down = False
    rounds = 0
    for rounds in range(1, 60):
        now[0] += 301  # past every backoff
        client.push(conn, hub, "default", "sender", primary=False, max_batches=1)
        if tid in hub.known and not conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE state='pending'").fetchone():
            break
    rows = [tuple(r) for r in conn.execute(
        "SELECT table_name,state,attempts,substr(last_error,1,40) FROM kanban_sync_outbox ORDER BY created_at,rowid")]
    healthy = [entry for entry in hub.log[2:]]
    print("P5 cap", cap, "rounds on a healthy Board:", rounds, "card on Board:", tid in hub.known,
          "refusals from the healthy Board:", sum(1 for _t, s, _r in healthy if s == "rejected"),
          "outbox:", rows)
    assert tid in hub.known, "the card never reached a healthy Board"
    assert not [r for r in rows if r[1] == "rejected"], rows
