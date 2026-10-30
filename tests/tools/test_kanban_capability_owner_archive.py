"""A worker may archive the capability owner cards of its own waiting cards. [daveey]

The relay path (``_local_owner`` in ``kanban_capability_routing``) opens the owner card as
``created_by="capability-relay"`` with ``idempotency_key=capability-owner:<card>:<need>``, so the
creator rule never matched the waiting card's task. Every probe of that route left a done owner
card behind that only an orchestrator could archive. Now the task whose card waited (``<card>`` is
the task itself or a card it created) may archive a done or unclaimed owner card; claimed owner
cards and other tasks' owner cards are still refused.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_capability_routing as kcr
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    db = home / "kanban.db"
    conn = connect(db)
    worker = kb.create_task(conn, title="relay live check")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_TASK", worker)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setattr(kcr, "_own_gateway", lambda conn: None)
    yield conn, worker
    conn.close()


def _relay_owner(conn, waiting_id, need="host:macbook"):
    """Open the owner card exactly as the relay path does on the waiting card's home gateway."""
    rid = f"{kcr.OWNER_PREFIX}{waiting_id}:{need}"
    kcr._local_owner(conn, kb.get_task(conn, waiting_id), {"generation": 1}, need=need, rid=rid,
                     self_gw="macbook", from_gw="worker-1", reason="run sw_vers on the MacBook")
    row = conn.execute("SELECT id, created_by FROM tasks WHERE idempotency_key = ?", (rid,)).fetchone()
    assert row is not None and row[1] == "capability-relay"
    return row[0]


def _finish(conn, tid):
    assert kb.complete_task(conn, tid, summary="sw_vers: 26.1")
    assert kb.get_task(conn, tid).status == "done"


def _archive(tid):
    from tools import kanban_tools as kt

    return json.loads(kt._handle_archive({"task_id": tid}))


def _archived_payload(conn, tid):
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'archived' "
                       "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def test_worker_archives_the_relay_owner_card_of_a_probe_it_created(board):
    conn, worker = board
    probe = kb.create_task(conn, title="PROBE", creator_task_id=worker)
    owner = _relay_owner(conn, probe)
    _finish(conn, owner)
    assert _archive(probe).get("ok") is True  # the probe goes first, as in the live check
    out = _archive(owner)
    assert out.get("ok") is True, out
    assert kb.get_task(conn, owner).status == "archived"
    assert (_archived_payload(conn, owner) or {}).get("via") == f"capability owner of {probe} by {worker}"


def test_worker_archives_an_unclaimed_owner_card_of_its_own_task(board):
    conn, worker = board
    owner = _relay_owner(conn, worker)
    assert kb.get_task(conn, owner).status in ("ready", "todo")
    assert _archive(owner).get("ok") is True
    assert kb.get_task(conn, owner).status == "archived"


def test_owner_card_of_another_tasks_probe_is_refused(board):
    conn, _worker = board
    other = kb.create_task(conn, title="other task")
    probe = kb.create_task(conn, title="PROBE", creator_task_id=other)
    owner = _relay_owner(conn, probe)
    _finish(conn, owner)
    out = _archive(owner)
    assert out.get("ok") is not True
    assert "may archive only" in json.dumps(out)
    assert kb.get_task(conn, owner).status == "done"


def test_claimed_owner_card_is_refused(board):
    conn, worker = board
    probe = kb.create_task(conn, title="PROBE", creator_task_id=worker)
    owner = _relay_owner(conn, probe)
    assert kb.claim_task(conn, owner) is not None
    out = _archive(owner)
    assert out.get("ok") is not True
    assert "is claimed" in json.dumps(out)
    assert kb.get_task(conn, owner).status == "running"


def test_a_plain_key_lookalike_does_not_widen_scope(board):
    """Only the ``capability-owner:`` prefix counts; any other key falls back to the creator rule."""
    conn, worker = board
    other = kb.create_task(conn, title="other task")
    tid = kb.create_task(conn, title="x", creator_task_id=other, idempotency_key=f"probe:{worker}:x")
    assert _archive(tid).get("ok") is not True
