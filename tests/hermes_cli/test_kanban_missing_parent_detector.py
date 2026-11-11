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
