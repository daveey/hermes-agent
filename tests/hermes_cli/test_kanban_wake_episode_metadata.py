"""Unrelated events keep their payloads; their reset evidence travels separately."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_wake_check as wc
from hermes_cli import kanban_wake_episode as episode
from tests.hermes_cli import test_kanban_wake_episode as base
from tests.hermes_cli import test_kanban_wake_episode_carry as carry

kanban_home = base.kanban_home
parked = base.parked


def _rows(conn, tid):
    return [(r[0], json.loads(r[1]) if r[1] else None)
            for r in conn.execute("SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]


def test_unrelated_outside_event_is_clean_and_reset_still_reaches_unobserved_episode(parked):
    conn, tid, clock = parked
    home = carry.replica(conn)  # never observes the episode started below
    try:
        base.result(parked, 0)
        assert wc.get_state(conn, tid)["episode_first_at"] == clock[0]
        home.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))  # direct/synced writer: no event
        before = len(_rows(home, tid))
        kb._append_event(home, tid, "workspace_archived", {"path": "/p"})
        kb._append_event(home, tid, "commented", {"n": 2})
        added = _rows(home, tid)[before:]
        assert [k for k, _ in added] == [episode.EVIDENCE_EVENT, "workspace_archived", "commented"]
        assert added[1][1] == {"path": "/p"} and added[2][1] == {"n": 2}
        assert added[0][1]["_wake_episode_v1"] is None and added[0][1]["_wake_episode_barriers"]
        carry.import_events(home, conn, tid)
        episode.reconcile(conn, tid)
        assert wc.get_state(conn, tid)["episode_first_at"] is None
    finally:
        home.close()


def test_exit_rearms_after_reentering_corridor(parked):
    conn, tid, _ = parked
    for _ in range(2):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
        kb._append_event(conn, tid, "commented")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    assert [k for k, _ in _rows(conn, tid)].count(episode.EVIDENCE_EVENT) == 2


@pytest.mark.parametrize("kind", ["blocked", "completed", "condition_met", "wake_check_stale"])
def test_outside_lifecycle_evidence_remains_inline(parked, kind):
    conn, tid, _ = parked
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
    before = len(_rows(conn, tid))
    kb._append_event(conn, tid, kind, {"reason": "test"})
    added = _rows(conn, tid)[before:]
    assert len(added) == 1
    assert added[0][0] == kind
    assert added[0][1]["reason"] == "test"
    assert added[0][1]["_wake_episode_v1"] is None
    assert added[0][1]["_wake_episode_barriers"]


def test_readopted_episode_outside_corridor_gets_fresh_reset(parked):
    conn, tid, clock = parked
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
    kb._append_event(conn, tid, "commented")
    first_barrier = episode.barriers.current(conn, tid)
    # A delayed peer observation can be adopted after the exit was recorded.
    snapshot = {"_wake_episode_v1": {"cmd": "false", "first_at": clock[0],
                                   "checks": 1, "last_stale_at": None},
                "_wake_episode_barriers": first_barrier}
    episode._apply_snapshot(conn, tid, json.dumps(snapshot))
    assert conn.execute("SELECT 1 FROM task_wake_episodes WHERE task_id = ?", (tid,)).fetchone()
    before = len(_rows(conn, tid))
    kb._append_event(conn, tid, "commented", {"n": 2})
    assert not conn.execute("SELECT 1 FROM task_wake_episodes WHERE task_id = ?", (tid,)).fetchone()
    added = _rows(conn, tid)[before:]
    assert [k for k, _ in added] == [episode.EVIDENCE_EVENT, "commented"]
    assert added[0][1]["_wake_episode_barriers"] != first_barrier


def test_probe_less_park_keeps_payload_and_clears_peer_probe(parked):
    conn, tid, _ = parked
    home = carry.replica(conn)
    try:
        base.result(parked, 0)
        home.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(home, tid, claimer="worker") is not None
        assert kb.schedule_task(home, tid, reason="SCHEDULED_UNTIL=2026-09-13T00:00:00Z rate-limit cooldown", clock_only=True, awaiting={"kind": "clock", "why": "fixture window"})
        rows = _rows(home, tid)
        scheduled = [p for k, p in rows if k == "scheduled"][-1]
        assert "_wake_episode_v1" not in scheduled
        assert rows[-2][0] == episode.EVIDENCE_EVENT and rows[-2][1]["_wake_episode_v1"] == {"park_cmd": None}
        carry.import_events(home, conn, tid)
        episode.reconcile(conn, tid)
        assert wc.get_state(conn, tid)["episode_first_at"] is None
    finally:
        home.close()
