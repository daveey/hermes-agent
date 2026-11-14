"""Pickup budget must use the executor's current graph, not just a stale replica."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as support

svc = support.svc
gws = support.gws


@pytest.mark.parametrize("mutation", ["link", "unlink", "done", "archived"])
def test_pickup_budget_respects_unpublished_local_waiter(gws, mutation):
    home, executor = gws
    home.enter()
    parent = kb.create_task(home.conn, title="foreign prerequisite", assignee="default", priority=0,
                            runs_on=[f"machine:{support.WORKER}"])
    other = kb.create_task(home.conn, title="foreign independent", assignee="default", priority=100,
                           runs_on=[f"machine:{support.WORKER}"])
    waiter = kb.create_task(home.conn, title="foreign urgent waiter", assignee="default", priority=900,
                            parents=[] if mutation == "link" else [parent], runs_on=[f"machine:{support.WORKER}"])
    # Keep the waiter out of the pickup lane regardless of its local mutation.
    home.conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (waiter,))
    home.conn.commit()
    home.round()
    executor.round()
    executor.enter()
    replica = wn.open_replica(executor.conn)
    assert replica is not None
    try:
        assert wn.import_cards(executor.conn, replica, [waiter]) == [waiter]
        if mutation == "link":
            executor.conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (parent, waiter))
        elif mutation == "unlink":
            executor.conn.execute("DELETE FROM task_links WHERE child_id=?", (waiter,))
        else:
            executor.conn.execute("UPDATE tasks SET status=? WHERE id=?", (mutation, waiter))
        executor.conn.commit()
        # No sync round: this is exactly the local-authoritative overlay the lane
        # sorter already uses. A one-card import budget must use it as well.
        picked, skips = wn._evaluate(executor.conn, replica, support.WORKER, mode="all", limit=1,
                                     profile_exists=None, full_detail="pickup batch full this tick")
        wanted, deferred = (parent, other) if mutation == "link" else (other, parent)
        assert picked == [wanted]
        assert skips[deferred][0] == "budget_exhausted"
        assert wn.import_cards(executor.conn, replica, picked) == [wanted]
        imported = kb.get_task(executor.conn, wanted)
        assert imported is not None and imported.priority == (0 if wanted == parent else 100)
    finally:
        replica.close()


@pytest.mark.parametrize("ready_priority,review_priority,enabled_pick", [(0, 1000, "review"), (1000, 0, "ready")])
def test_pickup_ranking_shares_score_across_lanes_and_keeps_eligibility(
    gws, monkeypatch, ready_priority, review_priority, enabled_pick,
):
    from hermes_cli import kanban_db_dispatch as dispatch

    home, executor = gws
    home.enter()
    ready = kb.create_task(home.conn, title="ready", assignee="default", priority=ready_priority,
                           runs_on=[f"machine:{support.WORKER}"])
    review = kb.create_task(home.conn, title="review", assignee="default", priority=review_priority,
                            runs_on=[f"machine:{support.WORKER}"])
    home.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (review,))
    home.conn.commit()
    home.round()
    executor.round()
    executor.enter()
    replica = wn.open_replica(executor.conn)
    assert replica is not None
    try:
        by_name = {"ready": ready, "review": review}
        # Review rows compete on score when review dispatch is on; when off they are ineligible.
        for enabled, expected in ((True, by_name[enabled_pick]), (False, ready)):
            monkeypatch.setattr(dispatch, "review_dispatch_enabled", lambda: enabled)
            picked, _ = wn._evaluate(executor.conn, replica, support.WORKER, mode="all", limit=1,
                                      profile_exists=None, full_detail="full")
            assert picked == [expected]
    finally:
        replica.close()


def test_pickup_pool_budget_imports_only_marked_cards_when_regular_full(gws):
    """A hub at regular 7/7 with idle priority slots copies only capacity/urgent foreign cards: a normal card
    could not start, so it records ``budget_exhausted`` instead of being imported."""
    from hermes_cli.kanban_admission_pools import PoolBudget

    home, executor = gws
    home.enter()
    normal = kb.create_task(home.conn, title="foreign normal", assignee="default", priority=1000,
                            runs_on=[f"machine:{support.WORKER}"])
    marked = kb.create_task(home.conn, title="foreign capacity", assignee="default", priority=0,
                            runs_on=[f"machine:{support.WORKER}"], admission_class="capacity",
                            admission_frees="frees: worker slots")
    home.round()
    executor.round()
    executor.enter()
    replica = wn.open_replica(executor.conn)
    assert replica is not None
    try:
        picked, skips = wn._evaluate(executor.conn, replica, support.WORKER, mode="all", limit=PoolBudget(0, 3),
                                     profile_exists=None, full_detail="regular pool full")
        assert picked == [marked]
        assert skips[normal] == ("budget_exhausted", "regular pool full")
        picked, _ = wn._evaluate(executor.conn, replica, support.WORKER, mode="all", limit=1,
                                 profile_exists=None, full_detail="full")
        assert picked == [normal]
    finally:
        replica.close()
    assert wn.pickup(executor.conn, budget=PoolBudget(0, 0, 3)) == []  # no free slot in any pool
    assert wn.pickup(executor.conn, budget=PoolBudget(0, 3)) == [marked]
    assert kb.get_task(executor.conn, normal) is None
