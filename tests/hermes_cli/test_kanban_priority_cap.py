"""Bound escalation at input and inherited ordering; capacity advice is evidence-led."""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_parent_unclaimed as notices
from hermes_cli.kanban_db_dispatch_queries import EFFECTIVE_PRIORITY
from hermes_cli.kanban_priority import PRIORITY_CAP, advice, lane_evidence
from tests.hermes_cli import test_kanban_parent_first_dispatch as helpers

conn = helpers.conn


def test_create_edit_and_relative_bump_cap(conn):
    task = helpers._ready(conn, "urgent", priority=10300)
    assert kb.get_task(conn, task).priority == PRIORITY_CAP
    kb.edit_task(conn, task, priority=990)
    kb.edit_task(conn, task, priority_bump=9)
    assert kb.get_task(conn, task).priority == 999
    kb.edit_task(conn, task, priority_bump=9000)
    assert kb.get_task(conn, task).priority == PRIORITY_CAP
    kb.edit_task(conn, task, priority=99999)
    assert kb.get_task(conn, task).priority == PRIORITY_CAP
    kb.edit_task(conn, task, priority=-10)
    assert kb.get_task(conn, task).priority == -10


@pytest.mark.parametrize("kwargs", [{"priority_bump": 0}, {"priority_bump": -1},
                                   {"priority_bump": 1, "priority": 2}])
def test_invalid_bump_is_atomic(conn, kwargs):
    task = helpers._ready(conn, "unchanged")
    with pytest.raises(ValueError):
        kb.edit_task(conn, task, title="changed", **kwargs)
    assert kb.get_task(conn, task).title == "unchanged"


def test_legacy_transitive_value_cannot_escape_cap(conn):
    root = helpers._ready(conn, "root", priority=-10)
    middle = kb.create_task(conn, title="middle", assignee="default", parents=[root])
    child = kb.create_task(conn, title="child", assignee="default", parents=[middle])
    conn.execute("UPDATE tasks SET priority=10300 WHERE id=?", (child,))
    conn.commit()
    assert conn.execute(f"SELECT {EFFECTIVE_PRIORITY} FROM tasks WHERE id=?", (root,)).fetchone()[0] == 1000
    assert kb.get_task(conn, root).priority == -10


def test_sync_fields_cap(conn):
    from hermes_cli.kanban_db_sync import _local_fields
    assert _local_fields(conn, "tasks", {"priority": 10300}, "id")["priority"] == 1000
    assert _local_fields(conn, "tasks", {"priority": -5}, "id")["priority"] == -5


def test_lane_median_and_notice_rendering(conn):
    low = helpers._ready(conn, "low", priority=1)
    high = helpers._ready(conn, "high", priority=900)
    kb.create_task(conn, title="different assignee", assignee="other", initial_status="running", priority=-100)
    assert lane_evidence(conn, low)["lane_median"] == 450.5
    for task, wants_bump in [(low, True), (high, False)]:
        assert notices.post_notice(conn, task, task, wait_key=1, waited_seconds=3600, ready_seconds=3600)
        raw = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                           (task, notices.NOTICE_KIND)).fetchone()[0]
        payload = json.loads(raw)
        from hermes_cli.kanban_chat_wait import notice_text
        for render in [notices.notice_text, notice_text]:
            text = render(payload)
            assert "capacity first" in text
            assert ("--priority-bump" in text) == wants_bump
    assert "--priority" not in advice(high)
    assert "--priority" not in notices.notice_text({"parent": high})


def test_inherited_priority_not_below_lane_median(conn):
    root = helpers._ready(conn, "root")
    helpers._ready(conn, "ordinary", priority=100)
    kb.create_task(conn, title="child", assignee="default", parents=[root], priority=900)
    assert lane_evidence(conn, root)["below_lane_median"] is False


def test_cap_bump_heads_queue_with_free_slots(conn, monkeypatch):
    from hermes_cli import kanban_queue_position as queue
    # Comparable ready episodes: the capped bump wins on priority, not card age.
    helpers._ready(conn, "older", priority=900, created_at=int(time.time()) - 60)
    task = helpers._ready(conn, "bumped", priority=800)
    kb.edit_task(conn, task, priority_bump=200)
    monkeypatch.setattr(queue, "_running_and_limit", lambda *_: (0, 7))
    result = queue.position(conn, kb.get_task(conn, task))
    assert result["ahead"] == 0
    assert result["running"] < result["limit"]


def test_cap_bump_does_not_erase_an_older_ready_episodes_score(conn, monkeypatch):
    from hermes_cli import kanban_queue_position as queue
    from hermes_cli.kanban_admission_score import rank

    older = helpers._ready(conn, "waiting", priority=900, created_at=int(time.time()) - 300)
    task = helpers._ready(conn, "bumped", priority=800)
    kb.edit_task(conn, task, priority_bump=200)
    monkeypatch.setattr(queue, "_running_and_limit", lambda *_: (0, 7))
    scored = rank(conn, [{"id": older}, {"id": task}])
    assert [(row["id"], parts.total, parts.age_buckets) for row, parts in scored] == [
        (older, 1025, 5), (task, 1000, 0),
    ]
    result = queue.position(conn, kb.get_task(conn, task))
    assert result["ahead"] == 1
    assert result["running"] < result["limit"]
    assert kb.get_task(conn, task).priority == PRIORITY_CAP
