"""A home clock wake cannot erase an executor observation it has not seen."""
import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_wake_check as wc
from hermes_cli.kanban_continuation import wake_due_continuations
from tests.hermes_cli import test_kanban_wake_episode as base
from tests.hermes_cli import test_kanban_wake_episode_carry as carry

kanban_home = base.kanban_home
parked = base.parked


def test_home_clock_wake_before_first_observation_arrives(parked):
    conn, tid, clock = parked
    home = carry.replica(conn)
    try:
        base.result(parked, 0)
        base.result(parked, 12)
        assert wake_due_continuations(home, now=clock[0] + 25 * base.HOUR) == [tid]
        base.repark((home, tid, clock))
        carry.import_events(home, conn, tid)
        assert base.result(parked, 48) == base.STALE
        assert wc.get_state(home, tid)["episode_first_at"] is None
        carry.import_events(conn, home, tid)
        from hermes_cli import kanban_wake_episode as episode
        episode.reconcile(home, tid)
        assert wc.get_state(home, tid)["episode_checks"] == 3
    finally:
        home.close()


def test_reset_before_unknown_repark_is_not_masked(parked):
    conn, tid, clock = parked
    home = carry.replica(conn)
    try:
        base.result(parked, 0)
        with kb.write_txn(home):
            home.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
            kb._append_event(home, tid, "blocked")
            home.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        base.repark((home, tid, clock))
        carry.import_events(home, conn, tid)
        assert base.result(parked, 48) is None
        assert wc.get_state(conn, tid)["episode_first_at"] == clock[0] + 48 * base.HOUR
    finally:
        home.close()


def test_relayed_registration_keeps_identical_probe_episode(parked, monkeypatch):
    from hermes_cli import kanban_timed_park as timed
    from hermes_cli import kanban_awaiting as awaiting
    # Isolate probe registration from the independent third-park owner escalation.
    monkeypatch.setattr(awaiting, "gate", lambda *args, **kwargs: kwargs["awaiting"])
    conn, tid, _ = parked
    home = carry.replica(conn)
    try:
        base.result(parked, 0)
        carry.import_events(conn, home, tid)
        monkeypatch.setattr(wc, "own_gateway", lambda conn: "home")
        from hermes_cli import kanban_wake_episode_barriers as barriers
        from hermes_cli import kanban_wake_episode as episode
        episode.reconcile(home, tid)
        initial_barriers = barriers.current(home, tid)
        for hour in (12, 24):
            with kb.write_txn(home):
                home.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
                assert timed.park(home, tid, reason=f"waiting for build {hour}", requested_kind="time",
                                  wake_check="exit 1", check_every=120, probe_host="executor",
                                  awaiting={"kind": "external", "ref": f"build-{hour}",
                                            "why": "next build produces the same flag"})
            assert kb.get_task(home, tid).status == "scheduled"
            assert wc.get_state(home, tid) is None
            assert barriers.current(home, tid) == initial_barriers
            carry.import_events(home, conn, tid)
            assert base.result(parked, hour) is None
        assert base.result(parked, 48) == base.STALE
        assert base.helpers._events(conn, tid, base.STALE)[0].payload["checks"] == 4
    finally:
        home.close()


def test_valid_snapshot_before_malformed_event_is_not_lost(parked):
    conn, tid, clock = parked
    data = {"cmd": "exit 1", "first_at": clock[0], "checks": 7, "last_stale_at": None}
    for payload in ({"_wake_episode_v1": data}, {"meta": {"_wake_episode_v1": None}}):
        conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                     (tid, "foreign", json.dumps(payload), clock[0]))
    assert base.result(parked, 48) == base.STALE
    assert base.helpers._events(conn, tid, base.STALE)[0].payload["checks"] == 8
