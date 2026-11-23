"""Real reserved-key and synthetic-parent capture-site scenarios. [daveey]

Return (table, op_id/task_id) custody keys. Public plan/apply/advance and push
callers execute the production INSERTs; call-through counters prove reachability.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_reserved_key as rk

OUTBOX, PARENTS = "kanban_sync_outbox", "kanban_sync_parent_envelopes"
KEY = {OUTBOX: "op_id", PARENTS: "task_id"}



# --- helpers -----------------------------------------------------------------------------------------

def _conn(board):
    return board[0] if isinstance(board, tuple) else board


def _ids(conn):
    return ({(OUTBOX, r[0]) for r in conn.execute("SELECT op_id FROM kanban_sync_outbox")}
            | {(PARENTS, r[0]) for r in conn.execute("SELECT task_id FROM kanban_sync_parent_envelopes")})


def spy(monkeypatch, module, name):
    """Call-through counter around the real function (never a replacement body)."""
    real, calls = getattr(module, name), []

    def wrapper(*a, **k):
        calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(module, name, wrapper)
    return calls



def _gateway_identity(conn, gateway):
    client.ensure_identity(conn, "default", gateway)


# --- 1/2: reserved_key._write_receipt and ._queue_children through supported plan/apply/advance ------------

def _reserved_setup(conn, monkeypatch):
    from tests.hermes_cli import test_kanban_sync_reserved_key as t
    _gateway_identity(conn, t.ME)
    svc = t.StrictService()
    tid, _other = t.bad_card(conn, svc, monkeypatch)        # reserved-key card whose ops the Board refused
    token = t.preview(conn, svc, tid)["token"]               # rk.plan
    return t, svc, tid, token


def case_reserved_write_receipt(board, monkeypatch):
    conn = _conn(board)
    t, svc, tid, token = _reserved_setup(conn, monkeypatch)
    calls = spy(monkeypatch, rk, "_write_receipt")
    before = _ids(conn)
    assert t.apply(conn, svc, tid, token)["action"] == "applied"   # rk.apply -> write_txn -> _write_receipt
    assert calls, "_write_receipt was not reached through rk.apply"
    receipt = rk.receipt_for_task(conn, tid)
    new = _ids(conn) - before
    # exactly the corrected-parent import op the function inserted (not an earlier rejected op)
    assert new == {(OUTBOX, receipt["parent_op_id"])}, new
    row = conn.execute("SELECT op, state, table_name FROM kanban_sync_outbox WHERE op_id=?",
                       (receipt["parent_op_id"],)).fetchone()
    assert tuple(row) == ("import", "pending", "tasks")
    return sorted(new)


def case_reserved_queue_children(board, monkeypatch):
    conn = _conn(board)
    t, svc, tid, token = _reserved_setup(conn, monkeypatch)
    assert t.apply(conn, svc, tid, token)["action"] == "applied"
    client.push(conn, svc.hub(t.ME), t.BOARD, t.ME, primary=False)      # parent accepted; its op leaves the outbox
    receipt = rk.receipt_for_task(conn, tid)
    assert not conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE op_id=?", (receipt["parent_op_id"],)).fetchone()
    calls = spy(monkeypatch, rk, "_queue_children")
    before = _ids(conn)
    report = rk.advance(conn, svc.hub(t.ME), t.BOARD, task_id=tid)[0]   # _queue_step -> _queue_children
    assert calls and report["advanced"] and report["children_queued"] > 0, report
    assert rk.receipt_for_task(conn, tid)["state"] == "children_queued"
    new = _ids(conn) - before
    manifest = {(OUTBOX, r[0]) for r in conn.execute(
        "SELECT op_id FROM kanban_sync_key_supersession_children WHERE receipt_id=?", (receipt["receipt_id"],))}
    assert new == manifest and len(new) == report["children_queued"], (new, manifest)
    return sorted(new)          # NOT pushed further: acked children would be deleted


# --- 3: compat.rejected_parent through the legacy push caller --------------------------------------------

def case_rejected_parent(board, monkeypatch):
    """A schema-refused synthetic parent materializes without claiming originality."""
    from hermes_cli import kanban_sync_compat as compat
    conn = _conn(board)
    _gateway_identity(conn, "sender")
    tid = kb.create_task(conn, title="rejected synthetic parent", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
    kb.add_comment(conn, tid, "author", "child needing the synthesized parent")
    sent = []

    def ops(_board, wire):
        sent.extend(wire)
        return {"results": [{"client_op_id": op["client_op_id"], "status": "rejected", "reason": "forbidden_field"}
                            for op in wire]}
    calls = spy(monkeypatch, compat, "rejected_parent")
    before = _ids(conn)
    client.push(conn, SimpleNamespace(ops=ops), "default", "sender", primary=False, max_batches=1)
    assert calls, "rejected_parent was not reached through client.push"
    parent_wire = next(op for op in sent if op["table"] == "tasks" and op["uid"] == tid)
    new = _ids(conn) - before
    row = conn.execute("SELECT * FROM kanban_sync_outbox WHERE row_uid=? AND table_name='tasks'", (tid,)).fetchone()
    assert row["op_id"] == parent_wire["client_op_id"] and (OUTBOX, row["op_id"]) in new
    assert row["wire_json"] is None and row["attempts"] == 1 and row["state"] == "rejected"
    assert not conn.execute("SELECT 1 FROM kanban_sync_parent_envelopes").fetchone()  # nothing else is custody
    return [(OUTBOX, row["op_id"])]


# --- 4: replay_sender._push_strict.record through the real strict push -----------------------------------

def _strict_journal(conn):
    from hermes_cli import kanban_replay_journal as journal
    from tests.hermes_cli.test_kanban_replay_journal import binding
    _gateway_identity(conn, "macbook")
    if sync_journal_id(conn) is None:
        with kbc.write_txn(conn):
            journal.initialize(conn, binding(),
                               board_id=conn.execute("SELECT board_id FROM kanban_sync_state").fetchone()[0])


def sync_journal_id(conn):
    from hermes_cli import kanban_db_sync as sync
    return (sync.sync_state(conn) or {}).get("journal_id")


def case_strict_record(board, monkeypatch):
    """Strict (journal-bound) push of a synthetic parent that the Board rejects: record() moves the exact synthetic
    parent into the outbox (INSERT with wire_json), then marks it rejected and retires the envelope.
    Uses the real client.push -> replay_sender.push -> _push_strict; ``record`` is a nested closure, so
    reachability is proven structurally (row id == journal pending op, envelope retired, out.rejected == 1)."""
    from hermes_cli import kanban_replay_journal as journal
    from tests.hermes_cli.test_kanban_replay_sender import Receiver
    conn = _conn(board)
    _strict_journal(conn)
    tid = kb.create_task(conn, title="strict rejected synthetic parent", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
    kb.add_comment(conn, tid, "author", "dependent child")
    receiver = Receiver()
    receiver.status = "rejected"
    before = _ids(conn)
    result = client.push(conn, receiver, "default", "macbook", primary=False, max_batches=1)
    assert result.rejected == 1, result
    parent = next(op for op in receiver.sent if op["table"] == "tasks" and op["uid"] == tid)
    new = _ids(conn) - before
    row = conn.execute("SELECT * FROM kanban_sync_outbox WHERE op_id=?", (parent["client_op_id"],)).fetchone()
    assert row is not None and (OUTBOX, row["op_id"]) in new
    assert row["state"] == "rejected" and row["attempts"] == 1 and row["wire_json"] is not None
    assert not conn.execute("SELECT 1 FROM kanban_sync_parent_envelopes").fetchone()  # custody moved, not duplicated
    assert journal.pending_wire(conn) is None
    return [(OUTBOX, row["op_id"])]


# --- 5: envelope.parent through the legacy push caller (lost reply keeps the immutable parent) -----------

def case_envelope_parent(board, monkeypatch):
    from hermes_cli import kanban_sync_envelope as envelopes
    from tests.hermes_cli.test_kanban_sync_envelope import LostReply
    conn = _conn(board)
    _gateway_identity(conn, "sender")
    tid = kb.create_task(conn, title="envelope synthetic parent", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
    kb.add_comment(conn, tid, "author", "child needs parent")
    calls = spy(monkeypatch, envelopes, "parent")
    before = _ids(conn)
    with pytest.raises(OSError, match="response lost"):
        client.push(conn, LostReply(), "default", "sender", primary=False)
    assert calls, "envelope.parent was not reached through client.push"
    new = {i for i in _ids(conn) - before if i[0] == PARENTS}
    assert new == {(PARENTS, tid)}, new
    assert json.loads(conn.execute("SELECT wire FROM kanban_sync_parent_envelopes WHERE task_id=?", (tid,))
                      .fetchone()[0])["uid"] == tid
    return sorted(new)


CASES = {
    "reserved_write_receipt": case_reserved_write_receipt,
    "reserved_queue_children": case_reserved_queue_children,
    "rejected_parent": case_rejected_parent,
    "strict_record": case_strict_record,
    "envelope_parent": case_envelope_parent,
}
