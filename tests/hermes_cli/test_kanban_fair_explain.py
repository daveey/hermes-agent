"""Queue diagnostics reflect the combined ready/review score order."""
import time

import pytest

from hermes_cli import kanban_admission_explain as explain
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_queue_position as position
from tests.hermes_cli import test_kanban_hub_admission as fixtures

board = fixtures.board


@pytest.mark.parametrize("review_enabled", [True, False])
def test_combined_diagnostics_follow_dispatch_ranking(board, monkeypatch, review_enabled):
    conn, home = board
    now = 1_800_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    fixtures.configure(home, limit=None, cap=7)
    monkeypatch.setattr(dispatch, "review_dispatch_enabled", lambda: review_enabled)
    old = fixtures.ready(conn, "old review", priority=0, created_at=now - 3600)
    new = fixtures.ready(conn, "new ready", priority=1000, created_at=now)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (old,))
    snap = explain.snapshot(conn)
    assert [row["id"] for row in snap["dispatch_order"]] == ([old, new] if review_enabled else [new])
    assert position._ahead(conn, kb.get_task(conn, new)) == int(review_enabled)
    rendered = "\n".join(explain._explain(conn, snap, new))
    assert f"combined position {2 if review_enabled else 1}/{2 if review_enabled else 1}" in rendered
    assert (f"ahead: {old}" in rendered) == review_enabled
    old_rendered = "\n".join(explain._explain(conn, snap, old))
    assert ("review dispatch disabled" in old_rendered) != review_enabled


@pytest.mark.parametrize("excluded_by", ["claim", "assignee", "pool"])
def test_combined_position_preserves_candidate_exclusions(board, monkeypatch, excluded_by):
    conn, home = board
    now = 1_800_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    fixtures.configure(home, limit=None, cap=7)
    monkeypatch.setattr(dispatch, "review_dispatch_enabled", lambda: True)
    old = fixtures.ready(conn, "old review", priority=0, created_at=now - 3600)
    new = fixtures.ready(conn, "new ready", priority=1000, created_at=now)
    column, value = {"claim": ("claim_lock", "held"), "assignee": ("assignee", "other"),
                     "pool": ("placement_policy", "worker-first-v1")}[excluded_by]
    with kb.write_txn(conn):
        conn.execute(f"UPDATE tasks SET status='review', {column}=? WHERE id=?", (value, old))
    assert position._ahead(conn, kb.get_task(conn, new)) == 0
