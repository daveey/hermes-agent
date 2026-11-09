"""A ready parent that other cards wait on is dispatched first, and its waiting chat hears when it is not. [daveey]

Defect kanban-447c98343f (2026-11-02): rollout t_01a19400313cfbda parked at 19:48 on its parent t_01a19f3049f432d5,
which sat ready from 19:04 to 21:04 behind 61 other priority-0 cards, because the dispatcher ordered the ready lane
by priority then age only. The waiting chat got no notice; David asked three times. These tests pin:
the dependent-aware lane order (``lane_rows``, ``list_tasks``, the worker-node candidates and the queue position
agree), one ``dependency_parent_unclaimed`` notice per wait, and the ``kanban_show`` queue hint.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kn
from hermes_cli.kanban_db_dispatch_queries import lane_rows


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.init_db()
    c = kbc.connect()
    yield c
    c.close()


def _ready(conn, title, *, priority=0, created_at=None, parents=()):
    tid = kb.create_task(conn, title=title, assignee="default", priority=priority, parents=parents,
                         initial_status="running")
    if created_at is not None:
        conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (created_at, tid))
        conn.commit()
    return tid


def _ids(rows):
    return [r["id"] for r in rows]


def test_ready_parent_with_waiting_child_dispatches_before_older_leaves(conn):
    base = int(time.time()) - 3600
    old1 = _ready(conn, "old leaf 1", created_at=base)
    old2 = _ready(conn, "old leaf 2", created_at=base + 1)
    parent = _ready(conn, "newest parent", created_at=base + 2)
    child = kb.create_task(conn, title="waiting child", assignee="default", parents=[parent])
    assert kb.get_task(conn, child).status == "todo"
    order = _ids(lane_rows(conn, "ready"))
    assert order[0] == parent, order
    assert order[1:] == [old1, old2]


def test_parent_inherits_waiting_childs_priority(conn):
    base = int(time.time()) - 3600
    parent = _ready(conn, "p0 parent", priority=0, created_at=base + 5)
    leaf = _ready(conn, "p5 leaf", priority=5, created_at=base)
    kb.create_task(conn, title="p10 child", assignee="default", priority=10, parents=[parent])
    assert _ids(lane_rows(conn, "ready"))[:2] == [parent, leaf]
    # Stored priority is untouched.
    assert kb.get_task(conn, parent).priority == 0


def test_explicit_priority_still_wins_over_waiting_child(conn):
    base = int(time.time()) - 3600
    parent = _ready(conn, "p0 parent", created_at=base)
    high = _ready(conn, "p5 leaf", priority=5, created_at=base + 1)
    kb.create_task(conn, title="p0 child", assignee="default", parents=[parent])
    assert _ids(lane_rows(conn, "ready"))[:2] == [high, parent]


def test_list_tasks_default_order_matches_lane(conn):
    base = int(time.time()) - 3600
    _ready(conn, "old leaf", created_at=base)
    parent = _ready(conn, "parent", created_at=base + 1)
    kb.create_task(conn, title="child", assignee="default", parents=[parent])
    assert [t.id for t in kb.list_tasks(conn, status="ready")] == _ids(lane_rows(conn, "ready"))


def test_queue_position_counts_the_dependent_order(conn):
    from hermes_cli import kanban_queue_position as kqp
    base = int(time.time()) - 3600
    _ready(conn, "old leaf", created_at=base)
    _ready(conn, "old leaf 2", created_at=base + 1)
    parent = _ready(conn, "parent", created_at=base + 2)
    kb.create_task(conn, title="child", assignee="default", parents=[parent])
    assert kqp._ahead(conn, kb.get_task(conn, parent)) == 0


def test_worker_node_candidates_put_waited_on_parent_first(conn):
    from hermes_cli import kanban_worker_node as wn
    base = int(time.time()) - 3600
    leaf = _ready(conn, "old leaf", created_at=base)
    parent = _ready(conn, "parent", created_at=base + 1)
    kb.create_task(conn, title="child", assignee="default", parents=[parent])
    have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    for col, ddl in (("created_on", "TEXT"), ("lease_holder", "TEXT"), ("lease_expires", "INTEGER")):
        if col not in have:  # a replica carries the sync columns; a plain test board does not
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")
    conn.execute("UPDATE tasks SET created_on='other-gw' WHERE id IN (?, ?)", (leaf, parent))
    conn.commit()
    assert [r["id"] for r in wn.candidates(conn, "me")][:2] == [parent, leaf]


# --- waiting-chat notice -------------------------------------------------------------------------------------


def _subscribe(conn, tid, chat="origin-chat"):
    kn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat, delivery_metadata={"reason": "create"})


def _age_wait(conn, child, parent, seconds):
    """Backdate the child's dependency wait and the parent's ready entry by ``seconds``."""
    then = int(time.time()) - seconds
    conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='dependency_wait'", (then, child))
    conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (then, parent))
    conn.execute("UPDATE tasks SET created_at=? WHERE id IN (?, ?)", (then, child, parent))
    conn.commit()


def _notices(conn, tid):
    from hermes_cli import kanban_parent_unclaimed as pu
    return [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, pu.NOTICE_KIND))]


@pytest.fixture
def waiting(conn):
    parent = _ready(conn, "Ask Eve sync backport")
    child = kb.create_task(conn, title="worker-first rollout", assignee="default", parents=[parent])
    _subscribe(conn, child)
    _age_wait(conn, child, parent, 31 * 60)
    return parent, child


def test_chat_owned_wait_on_unclaimed_parent_notifies_once(conn, waiting):
    from hermes_cli import kanban_parent_unclaimed as pu
    parent, child = waiting
    assert pu.sweep(conn) == 1
    notices = _notices(conn, child)
    assert len(notices) == 1
    assert notices[0]["parent"] == parent
    text = pu.notice_text(notices[0])
    assert parent in text and "bump" in text and "re-place" in text and "here" in text
    assert pu.sweep(conn) == 0
    assert len(_notices(conn, child)) == 1


def test_claimed_parent_sends_no_notice(conn, waiting):
    from hermes_cli import kanban_parent_unclaimed as pu
    parent, child = waiting
    conn.execute("UPDATE tasks SET claim_lock='hub:1' WHERE id=?", (parent,))
    conn.commit()
    assert pu.sweep(conn) == 0
    assert _notices(conn, child) == []


def test_short_wait_sends_no_notice(conn):
    from hermes_cli import kanban_parent_unclaimed as pu
    parent = _ready(conn, "parent")
    child = kb.create_task(conn, title="child", assignee="default", parents=[parent])
    _subscribe(conn, child)
    _age_wait(conn, child, parent, 10 * 60)
    assert pu.sweep(conn) == 0


def test_child_without_a_chat_sends_no_notice(conn):
    from hermes_cli import kanban_parent_unclaimed as pu
    parent = _ready(conn, "parent")
    child = kb.create_task(conn, title="child", assignee="default", parents=[parent])
    _age_wait(conn, child, parent, 31 * 60)
    assert pu.sweep(conn) == 0


def test_notice_kind_is_delivered_by_both_notifiers():
    from gateway import kanban_watchers_notifier as gw
    from hermes_cli import kanban_parent_unclaimed as pu
    from tui_gateway import session_notifications as sn
    assert pu.NOTICE_KIND in gw.TERMINAL_KINDS
    assert pu.NOTICE_KIND in sn._KANBAN_NOTIFY_KINDS
    assert pu.NOTICE_KIND in sn._KANBAN_EVENT_FORMATTERS


def test_dispatch_tick_runs_the_sweep(conn, waiting, monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd
    _parent, child = waiting
    monkeypatch.setattr(kbd, "_tick_spawn_budget", lambda *a, **k: (False, 0))
    kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda *a, **k: None)
    assert len(_notices(conn, child)) == 1


# --- kanban_show hint ----------------------------------------------------------------------------------------


def test_kanban_show_hints_a_long_ready_card(conn, waiting, monkeypatch):
    from tools import kanban_show
    parent, _child = waiting
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    out = json.loads(kanban_show.show({"task_id": parent}))
    hint = out.get("queue_hint") or ""
    assert "a card that can't start is no fix" in hint
    assert "bump" in hint and "re-place" in hint


def test_kanban_show_has_no_hint_for_a_fresh_ready_card(conn, monkeypatch):
    from tools import kanban_show
    tid = _ready(conn, "fresh")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    out = json.loads(kanban_show.show({"task_id": tid}))
    assert not out.get("queue_hint")
