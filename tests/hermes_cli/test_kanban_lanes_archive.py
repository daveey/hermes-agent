"""Automatic retirement must never release work or discard human custody."""
import json
import time

import pytest

from hermes_cli import kanban_db as kb, kanban_lanes as lanes, kanban_lanes_plan as plan
from tests.hermes_cli import test_kanban_lanes as base

conn = base.conn
configured = base.configured
person = base.person


def held(conn, *, creator="core-ship-backstop"):
    card = kb.create_task(conn, title="Retro review", assignee="default", created_by=creator,
                          tenant="hermes", idempotency_key="core-ship-backstop:example:abc")
    conn.execute("UPDATE tasks SET status='triage', block_kind=?, created_at=? WHERE id=?",
                 (lanes.GATE_KIND, int(time.time()) - 9 * 86400, card))
    kb._append_event(conn, card, lanes.GATED, {"lane": "hermes"})
    conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind=?",
                 (int(time.time()) - 9 * 86400, card, lanes.GATED))
    conn.commit()
    return card


def archive(conn, card, **extra):
    return plan.apply(conn, [{"action": "archive", "card": card, "basis": "stale",
                              "reason": "Expired informational review", **extra}])


def test_stale_informational_archive_has_audited_basis_and_no_admission(conn, configured):
    card = held(conn)
    result = archive(conn, card)
    assert result["applied"], result
    assert kb.get_task(conn, card).status == "archived"
    event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='archived'",
                         (card,)).fetchone()
    assert json.loads(event[0])["basis"] == "stale"
    assert lanes.admitted_today(conn, "hermes") == 0


@pytest.mark.parametrize("guard", ["human", "running", "child", "ask", "claimed", "recent", "incident", "basis", "gate_recent", "gate_missing"])
def test_archive_refuses_protected_or_unproved_cards(conn, configured, guard):
    card = held(conn, creator="user" if guard == "human" else "core-ship-backstop")
    updates = {
        "running": ("status", "running"),
        "ask": ("human_request", '{"action":"Decide"}'),
        "claimed": ("claim_lock", "worker:123"),
        "recent": ("created_at", int(time.time())),
        "incident": ("created_by", "ci-main-watch"),
    }
    if guard in updates:
        column, value = updates[guard]
        conn.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, card))
        conn.commit()
    if guard == "gate_recent":
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind=?",
                     (int(time.time()), card, lanes.GATED))
        conn.commit()
    if guard == "gate_missing":
        conn.execute("DELETE FROM task_events WHERE task_id=? AND kind=?", (card, lanes.GATED))
        conn.commit()
    if guard == "child":
        child = kb.create_task(conn, title="waiting implementation", assignee="default", created_by="user")
        kb.link_tasks(conn, card, child)
    before = kb.get_task(conn, card).status
    result = archive(conn, card, **({"basis": "because I said so"} if guard == "basis" else {}))
    assert not result["applied"] and result["rejected"], result
    assert kb.get_task(conn, card).status == before
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='archived'", (card,)).fetchone()
