"""A worker repins a worker-first card its own task created via kanban_move(task_id=...). [daveey]

The no-pool-worker notice tells the creator to repin the card. A creator that is another card's worker could
not: its shell is fenced from ``hermes kanban move`` and ``kanban_move`` refused any task but its own, so the
card stayed queued forever (live: fixture t_01a1a46cbd08b275, created by t_01a1a10ffdb8433c). The grant is
narrow: only the explicit origin repin, only for a card the caller's task created, never a running card.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_placement as wp
from hermes_cli import kanban_worker_repin as repin
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def board(tmp_path, monkeypatch):
    db = tmp_path / "board.db"
    conn = connect(db)
    owner = kb.create_task(conn, title="owner")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_TASK", owner)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []

    def fake_repin(conn_, task_id, labels, reason, *, hub=None):
        calls.append((task_id, labels, reason))
        return {"ok": True, "runs_on": labels, "repinned": True, "readback": "confirmed"}

    monkeypatch.setattr(repin, "repin_to_origin", fake_repin)
    yield conn, owner, calls
    conn.close()


def _move(task_id):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_move({"task_id": task_id, "runs_on": ["machine:hub"],
                                       "reason": "no pool worker can run it", "checkpoint": "n/a"}))


def test_creator_worker_repins_its_queued_worker_first_card(board):
    conn, owner, calls = board
    card = kb.create_task(conn, title="fixture", creator_task_id=owner, placement_policy=wp.POLICY)
    out = _move(card)
    assert out.get("ok") is True, out
    assert calls == [(card, ["machine:hub"], "no pool worker can run it")]
    # The caller's own card is untouched: repinning another card never ends or re-queues this run.
    assert not [e for e in kb.list_events(conn, owner) if e.kind in ("continued", "moved", "worker_moved")]


@pytest.mark.parametrize("source_path", ["/example/owned-source", None, ""])
def test_creator_repin_refuses_explicit_source_before_any_side_effect(board, monkeypatch, source_path):
    from unittest.mock import Mock
    from tools import kanban_tools as kt
    from tools import kanban_tools_source as source_tools

    conn, owner, calls = board
    card = kb.create_task(conn, title="fixture", creator_task_id=owner, placement_policy=wp.POLICY)
    before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (card,)).fetchone())
    events = kb.list_events(conn, card)
    register = Mock(side_effect=AssertionError("other-card repin must not attach source"))
    monkeypatch.setattr(source_tools, "register_for_ending", register)
    out = json.loads(kt._handle_move({
        "task_id": card, "runs_on": ["machine:hub"], "reason": "no pool worker can run it",
        "checkpoint": "n/a", "source_path": source_path,
    }))
    assert "source_path is only valid for the owning worker's ending" in out.get("error", ""), out
    assert calls == []
    register.assert_not_called()
    assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (card,)).fetchone()) == before
    assert kb.list_events(conn, card) == events
    assert kb.list_attachments(conn, card) == []
    # This exact creator/target is otherwise authorized: no adjacent refusal can
    # mask removal of the source_path fence. Omitting the field still repins.
    control = _move(card)
    assert control.get("ok") is True, control
    assert calls == [(card, ["machine:hub"], "no pool worker can run it")]
    register.assert_not_called()


def test_worker_may_not_repin_a_card_another_task_created(board):
    conn, owner, calls = board
    other = kb.create_task(conn, title="other owner")
    card = kb.create_task(conn, title="foreign", creator_task_id=other, placement_policy=wp.POLICY)
    out = _move(card)
    assert out.get("ok") is not True and owner in out["error"], out
    assert calls == []


def test_creator_worker_may_not_relabel_a_card_without_worker_first_policy(board):
    conn, owner, calls = board
    card = kb.create_task(conn, title="plain", creator_task_id=owner)
    out = _move(card)
    assert out.get("ok") is not True and "worker-first" in out["error"], out
    assert calls == []


def test_creator_worker_may_not_repin_a_running_card(board):
    conn, owner, calls = board
    card = kb.create_task(conn, title="running", creator_task_id=owner, placement_policy=wp.POLICY)
    # A worker-first card is claimed through the Board lease, so set the running state the lease leaves.
    with conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (card,))
    out = _move(card)
    assert out.get("ok") is not True, out
    assert calls == []
