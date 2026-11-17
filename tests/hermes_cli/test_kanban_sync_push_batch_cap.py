"""A push request never exceeds the Board's 200-op limit (2026-11-17 incident).

``push`` selects up to PUSH_BATCH pending rows and sends the unacknowledged cards those rows belong to ahead of
them. With a full batch and one such card the request was 201 ops; the Board answered 400 "ops must be a list of
<= 200", the same rows were selected again, and the hub's outbox grew to 80,000 ops over 7 hours. [daveey]"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

BOARD, ME = "default", "hub"


@pytest.fixture
def live(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / "kanban.db")
    client.ensure_identity(conn, BOARD, ME)
    try:
        yield conn
    finally:
        conn.close()


def _pending(conn):
    return conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state = 'pending'").fetchone()[0]


def _full_batch_behind_an_unacked_card(conn, extra=0):
    """PUSH_BATCH + ``extra`` pending comment rows on one card whose own upsert is not in the outbox and that the
    Board has never acknowledged (sync_version NULL)."""
    tid = kb.create_task(conn, title="card", assignee="worker")
    for i in range(client.PUSH_BATCH + extra):
        kb.add_comment(conn, tid, "default", f"comment {i}")
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox WHERE table_name != 'task_comments'")
        conn.execute("UPDATE tasks SET sync_version = NULL WHERE id = ?", (tid,))
    assert _pending(conn) == client.PUSH_BATCH + extra
    return tid


def test_fake_board_refuses_more_than_200_ops(live):
    svc = FakeService()
    with pytest.raises(client.HubError) as err:
        svc.hub(ME).ops(BOARD, [{"client_op_id": str(i)} for i in range(201)])
    assert err.value.status == 400


@pytest.mark.parametrize("extra", [0, 50])
def test_full_batch_plus_unacked_parent_stays_within_the_limit_and_drains(live, extra):
    tid = _full_batch_behind_an_unacked_card(live, extra)
    svc = FakeService()
    out = client.push(live, svc.hub(ME), BOARD, ME, primary=False)
    sizes = [len(ops) for kind, _board, ops in svc.calls if kind == "ops"]
    assert sizes and max(sizes) <= client.PUSH_BATCH
    assert sizes[0] == client.PUSH_BATCH  # the parent plus 199 rows, not 201
    assert svc.calls[0][2][0]["table"] == "tasks" and svc.calls[0][2][0]["uid"] == tid  # parent still goes first
    assert out.sent == client.PUSH_BATCH + extra
    assert _pending(live) == 0
    assert tid in svc.board(BOARD)["rows"]["tasks"]


def test_a_card_whose_own_row_is_trimmed_off_does_not_push_the_request_over_the_limit(live):
    """Review of PR #3484: X is unacked with 100 comments and no outbox row of its own; T is unacked with 99
    comments and its own tasks upsert as the 200th row. The first parent pass finds 1 parent (X), the trim to 199
    rows drops T's own row, and T then needs a synthesized parent too: 199 + 2 = 201 unless the fit is re-checked."""
    x = kb.create_task(live, title="x", assignee="worker")
    for i in range(100):
        kb.add_comment(live, x, "default", f"x {i}")
    t = kb.create_task(live, title="t", assignee="worker")
    for i in range(99):
        kb.add_comment(live, t, "default", f"t {i}")
    with kbc.write_txn(live):
        own = dict(live.execute("SELECT * FROM kanban_sync_outbox WHERE table_name = 'tasks' AND row_uid = ? "
                                "ORDER BY created_at, rowid LIMIT 1", (t,)).fetchone())
        live.execute("DELETE FROM kanban_sync_outbox WHERE table_name != 'task_comments'")
        own["created_at"] = live.execute("SELECT MAX(created_at) FROM kanban_sync_outbox").fetchone()[0] + 1
        live.execute(f"INSERT INTO kanban_sync_outbox ({', '.join(own)}) VALUES ({', '.join('?' * len(own))})",
                     list(own.values()))
        live.execute("UPDATE tasks SET sync_version = NULL WHERE id IN (?, ?)", (x, t))
    assert _pending(live) == client.PUSH_BATCH
    svc = FakeService()
    client.push(live, svc.hub(ME), BOARD, ME, primary=False)
    client.push(live, svc.hub(ME), BOARD, ME, primary=False)  # a trimmed short selection finishes next round
    sizes = [len(ops) for kind, _board, ops in svc.calls if kind == "ops"]
    assert sizes and max(sizes) <= client.PUSH_BATCH
    assert _pending(live) == 0
    assert {x, t} <= set(svc.board(BOARD)["rows"]["tasks"])
