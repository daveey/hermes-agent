"""Cross-lane fairness must not weaken marked-only pool eligibility."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from tests.hermes_cli import test_kanban_hub_admission as fixtures

board = fixtures.board


@pytest.mark.parametrize("older_lane", ["ready", "review"])
@pytest.mark.parametrize("hub", [False, True])
def test_one_slot_follows_ready_age_across_lanes(board, monkeypatch, all_assignees_spawnable, older_lane, hub):
    conn, home = board
    now = 1_800_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    fixtures.configure(home, limit=None, cap=7)
    from tests.hermes_cli.kanban_review_fixture import completed_source
    completed_source(conn)
    if hub:
        fixtures._enable(home)
        fixtures.advisory(home)
        fixtures.locked(monkeypatch, fixtures.fake_sample(10))
    monkeypatch.setattr(dispatch, "_memory_pressure_level", lambda: "normal")
    monkeypatch.setattr(dispatch, "review_dispatch_enabled", lambda: True)
    older = fixtures.ready(conn, "older", priority=0, created_at=now - 3600, review=older_lane == "review")
    fresh = fixtures.ready(conn, "fresh", priority=1000, created_at=now, review=older_lane != "review")
    review_id = older if older_lane == "review" else fresh
    from hermes_cli.kanban_review_model import implementer_models
    assert implementer_models(conn, kb.get_task(conn, review_id)) == {"claude-sonnet-5-5"}
    spawned = []
    dispatch.dispatch_once(conn, spawn_fn=lambda candidate, workspace, board=None: spawned.append(candidate.id),
                           max_spawn=1, max_in_progress=7)
    assert spawned == [older]
    assert kb.get_task(conn, fresh).status != "running"


def test_hub_aging_crosses_marked_partition_without_borrowing_pool(board):
    from hermes_cli import kanban_admission_pools as pools
    conn, home = board
    now = int(time.time())
    older = fixtures.ready(conn, "older", priority=0, created_at=now - 3600)
    marked = fixtures.ready(conn, "marked", created_at=now, admission_class="urgent")
    fixtures._enable(home)
    assert [row["id"] for row in pools.lane_rows(conn, "ready")] == [older, marked]
    budget = pools.PoolBudget(0, 1)
    assert not budget.admits("normal")
    assert budget.admits("urgent")
