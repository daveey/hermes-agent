"""A rehomed human stop is authoritative before its history page arrives."""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_remote_repark as helpers

kanban_home = helpers.kanban_home


@pytest.fixture
def home(kanban_home):
    for conn in helpers.support.synced_conn(kanban_home):
        conn.execute("UPDATE kanban_sync_state SET gateway_id = 'hub', mode = 'primary'")
        conn.commit()
        yield conn


ASK = {
    "action": "Reply A or B. Unblock = apply your choice; archive = drop this work.",
    "why_human": "Only the user can authorize the additional spend.",
    "interaction": "reply",
    "scope": "One additional paid run.",
    "evidence": "The approved budget is exhausted.",
}


@pytest.mark.parametrize("history", ["absent", "older-unblock"])
def test_rehomed_human_block_waits_for_answer_not_history(home, history):
    now = int(time.time())
    feed, tid = helpers.Feed(home), "t_01a1e31e00000001"
    feed.apply(feed.row(tid, title="budget decision", status="blocked", created_on="hub",
                        created_at=now - 10000, workspace_kind="scratch", assignee="default",
                        block_kind="needs_input", human_request=json.dumps(ASK)))
    if history == "older-unblock":
        feed.apply(feed.event(tid, "unblocked", now - 1000, {}))
    assert kb.recompute_ready(home) == 0
    assert kb.get_task(home, tid).status == "blocked"
    assert kb.get_task(home, tid).human_request == ASK
    feed.apply(feed.event(tid, "blocked", now - 500, {"kind": "needs_input", "human_request": ASK}))
    assert kb.recompute_ready(home) == 0
    assert kb.unblock_task(home, tid)
    assert kb.get_task(home, tid).status == "ready"
    assert kb.get_task(home, tid).human_request is None


@pytest.mark.parametrize("stale_kind", [None, "needs_input"])
def test_rehomed_schedule_keeps_wait_while_first_park_is_imported(home, stale_kind):
    from hermes_cli import kanban_continuation as kc

    now = int(time.time())
    feed, tid = helpers.Feed(home), "t_01a1e31e00000002"
    feed.apply(feed.row(tid, title="future wait", status="scheduled", created_on="hub",
                        created_at=now - 10000, workspace_kind="scratch", assignee="default",
                        block_kind=stale_kind))
    # The initial history page can contain only old metadata, not the park yet.
    feed.apply(feed.event(tid, "created", now - 200000, {}))
    assert kc.wake_due_continuations(home, now=now) == []
    assert kb.get_task(home, tid).status == "scheduled"
    wake_at = now + 86400
    feed.apply(feed.event(tid, "scheduled", now - 1000, {"wake_at": wake_at}))
    assert kc.wake_due_continuations(home, now=now) == []
    assert kb.get_task(home, tid).status == "scheduled"
    assert kc.wake_due_continuations(home, now=wake_at) == [tid]


def test_missing_imported_park_has_a_stable_bounded_backstop(home):
    from hermes_cli import kanban_continuation as kc
    from hermes_cli.kanban_restore_chat_subs import restored_at

    now = int(time.time())
    feed, tid = helpers.Feed(home), "t_01a1e31e00000003"
    feed.apply(feed.row(tid, title="lost park", status="scheduled", created_on="hub",
                        created_at=now - 200000, workspace_kind="scratch", assignee="default"))
    receipt = restored_at(home, tid)
    assert receipt is not None
    feed.apply(feed.event(tid, "created", now - 200000, {}))
    deadline = receipt + kc.SUPERSEDED_PARK_GRACE_SECONDS
    assert kc.park_state(home, tid)["parked_at"] == receipt
    assert kc.wake_due_continuations(home, now=deadline - 1) == []
    assert kc.wake_due_continuations(home, now=deadline) == [tid]
    assert helpers._woken(home, tid)[-1]["park_missing"] is True


def test_unheld_imported_failure_can_still_recover(home):
    feed, tid = helpers.Feed(home), "t_01a1e31e00000004"
    feed.apply(feed.row(tid, title="retry", status="blocked", created_on="hub",
                        created_at=int(time.time()), workspace_kind="scratch", assignee="default",
                        block_kind="transient"))
    assert kb.recompute_ready(home) == 1
    assert kb.get_task(home, tid).status == "ready"
