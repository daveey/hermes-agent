"""Expired missing dependencies get agent-owned repair work, not just a red child."""
import json

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_cross_gateway_parents as support

local = support.local


def test_expired_missing_parent_files_one_repair_card(local):
    child = kb.create_task(local, title="dependent", assignee="worker")
    support._link_raw(local, "t_unknown", child)
    kb.recompute_ready(local)
    assert not local.execute("SELECT id FROM tasks WHERE created_by = 'missing-parent-detector'").fetchall()
    with kb.write_txn(local):
        local.execute("UPDATE task_events SET created_at = created_at - 601 WHERE task_id = ?", (child,))
    kb.recompute_ready(local)
    cards = local.execute("SELECT * FROM tasks WHERE created_by = 'missing-parent-detector'").fetchall()
    assert len(cards) == 1
    repair = cards[0]
    assert repair['status'] == 'ready'
    assert repair['human_request'] is None
    assert child in repair['body'] and 't_unknown' in repair['body']
    assert kb.parent_ids(local, repair['id']) == []
    event = json.loads(support._events(local, child, 'blocked')[-1]['payload'])
    assert event['repair_task_id'] == repair['id']
    kb.recompute_ready(local)
    assert local.execute("SELECT count(*) FROM tasks WHERE created_by = 'missing-parent-detector'").fetchone()[0] == 1


def _expired(local):
    child = kb.create_task(local, title="dependent", assignee="worker")
    support._link_raw(local, "t_unknown", child)
    kb.recompute_ready(local)
    with kb.write_txn(local):
        local.execute("UPDATE task_events SET created_at = created_at - 601 WHERE task_id = ?", (child,))
    return child


@pytest.mark.parametrize('closed', [False, True])
def test_reopened_child_reuses_open_repair_but_replaces_closed(local, closed):
    child = _expired(local)
    kb.recompute_ready(local)
    first = local.execute("SELECT id FROM tasks WHERE created_by = 'missing-parent-detector'").fetchone()[0]
    with kb.write_txn(local):
        if closed:
            local.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (first,))
        local.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))
    kb.recompute_ready(local)
    repairs = local.execute("SELECT id FROM tasks WHERE created_by = 'missing-parent-detector'").fetchall()
    assert len(repairs) == (2 if closed else 1)


def test_failed_repair_admission_rolls_back_block(local, monkeypatch):
    child = _expired(local)
    def fail(*args, **kwargs):
        raise RuntimeError('injected admission failure')
    monkeypatch.setattr(kb, 'create_task', fail)
    with pytest.raises(RuntimeError, match='injected admission'):
        kb.recompute_ready(local)
    assert kb.get_task(local, child).status == 'todo'
    assert not support._events(local, child, 'blocked')


def test_returned_parent_does_not_raise_repair(local):
    parent = kb.create_task(local, title="parent", assignee="worker")
    child = kb.create_task(local, title="child", assignee="worker", parents=[parent])
    kb.recompute_ready(local)
    assert kb.parent_states(local, child) == [(parent, 'ready')]
    assert not local.execute("SELECT id FROM tasks WHERE created_by = 'missing-parent-detector'").fetchall()


# --- a streak belongs to the parents it names and ends at an unblock or a link ---------------------------------
# 2026-11-12 22:32 PT, David-MBP: t_01a1cfbe6c8d46e7 was blocked 127 s after worker-4 linked a brand-new parent,
# because its 21:09 streak (another parent, unblocked at 21:37 with no parent_found) still counted as open.

kanban_home, svc, live = support.kanban_home, support.svc, support.live


def _age(conn, child):
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET created_at = created_at - ? WHERE task_id = ? AND kind = 'parent_missing'",
                     (kb.PARENT_MISSING_BLOCK_SECONDS + 1, child))


def _repairs(conn, child):
    return [r[0] for r in conn.execute(
        "SELECT id FROM tasks WHERE created_by = 'missing-parent-detector' AND title LIKE ? ORDER BY rowid",
        (f"%{child}",))]


def _missing_named(conn, child):
    return json.loads(support._events(conn, child, "parent_missing")[-1]["payload"])["parents"]


def test_a_parent_linked_after_an_unblock_starts_its_own_streak(live, svc, kanban_home):
    child = kb.create_task(live, title="rework", assignee="worker")
    support._round(kanban_home, svc)
    support._link_raw(live, "t_a", child)
    support._stamp_replica(support.time.time())  # a fresh replica: the Board really lacks t_a for now
    kb.recompute_ready(live)
    _age(live, child)
    kb.recompute_ready(live)
    assert kb.get_task(live, child).status == "blocked"
    [first] = _repairs(live, child)
    with kb.write_txn(live):  # t_a turns up; the repair card finishes and unblocks the child
        live.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_a', 'a', 'done', 0)")
        live.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (first,))
    assert kb.unblock_task(live, child)
    assert kb.get_task(live, child).status == "ready"  # straight to ready: no pass ever wrote parent_found
    support._link_raw(live, "t_b", child)  # worker-4 links a parent created a second ago, not mirrored yet
    with kb.write_txn(live):
        kb._append_event(live, child, "linked", {"parent": "t_b", "child": child})
    kb.recompute_ready(live)
    assert kb.get_task(live, child).status == "todo"  # before the fix: blocked at once on the 82-min-old streak
    assert _missing_named(live, child) == ["t_b"]
    kb.recompute_ready(live)
    assert kb.get_task(live, child).status == "todo"
    _age(live, child)
    kb.recompute_ready(live)
    kb.recompute_ready(live)
    assert kb.get_task(live, child).status == "blocked"
    assert len(support._events(live, child, "blocked")) == 2
    assert len(_repairs(live, child)) == 2  # exactly one new repair card, for t_b


@pytest.mark.parametrize("linked_event", [True, False])
def test_a_newly_missing_parent_waits_its_own_window(local, linked_event):
    """No unblock: an old open streak for t_a must not expire t_b, whether the link arrived with a ``linked``
    event or only as a row (then the streak's parent list, which lacks t_b, is what reopens it)."""
    child = kb.create_task(local, title="dependent", assignee="worker")
    with kb.write_txn(local):
        local.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_a', 'a', 'running', 0)")
    support._link_raw(local, "t_a", child)
    with kb.write_txn(local):  # a streak for t_a that no pass closed
        kb._append_event(local, child, "parent_missing", {"parents": ["t_a"]})
    _age(local, child)
    support._link_raw(local, "t_b", child)
    if linked_event:
        with kb.write_txn(local):
            kb._append_event(local, child, "linked", {"parent": "t_b", "child": child})
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "todo"  # before the fix: blocked at once
    assert _missing_named(local, child) == ["t_b"]
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "todo"
    _age(local, child)
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "blocked"
    assert len(_repairs(local, child)) == 1


def test_a_streak_naming_more_parents_still_covers_the_ones_left(local):
    child = kb.create_task(local, title="dependent", assignee="worker")
    support._link_raw(local, "t_a", child)
    support._link_raw(local, "t_b", child)
    kb.recompute_ready(local)
    assert sorted(_missing_named(local, child)) == ["t_a", "t_b"]
    with kb.write_txn(local):
        local.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_a', 'a', 'running', 0)")
    _age(local, child)
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "blocked"
    assert len(support._events(local, child, "parent_missing")) == 1
