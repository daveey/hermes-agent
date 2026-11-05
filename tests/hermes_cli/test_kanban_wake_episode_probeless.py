"""Probe-less parks add evidence only when some probe could still need clearing."""
import json
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as wc
from hermes_cli import kanban_wake_episode as episode
from tests.hermes_cli import test_kanban_wake_check as helpers
from tests.hermes_cli import test_kanban_wake_episode as base
from tests.hermes_cli import test_kanban_wake_episode_carry as carry

kanban_home = base.kanban_home
parked = base.parked
SOAK = "SCHEDULED_UNTIL=2026-09-13T00:00:00Z soak"


def _kinds(conn, tid):
    return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]


def _park_without_probe(conn, tid):
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    before = len(_kinds(conn, tid))
    assert kb.schedule_task(conn, tid, reason=SOAK, clock_only=True)
    return [k for k in _kinds(conn, tid)[before:] if k not in {"claimed", "commented"}]


def _episode(conn, tid):
    return conn.execute("SELECT cmd FROM task_wake_episodes WHERE task_id = ?", (tid,)).fetchone()


def test_never_probed_park_adds_no_evidence(kanban_home):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        before = len(_kinds(conn, tid))
        assert kb.schedule_task(conn, tid, reason=SOAK, clock_only=True)
        assert [k for k in _kinds(conn, tid)[before:] if k != "commented"] == ["scheduled"]


def test_repeated_probe_less_park_evidence_is_emitted_once(parked):
    conn, tid, _ = parked
    assert _park_without_probe(conn, tid) == [episode.EVIDENCE_EVENT, "scheduled"]
    assert _park_without_probe(conn, tid) == ["scheduled"]  # cleared probe already carried


def test_local_episode_without_probe_record_still_carries_evidence(parked):
    conn, tid, _ = parked
    base.result(parked, 0)
    conn.execute("DELETE FROM task_wake_episode_cmd WHERE task_id = ?", (tid,))
    assert _episode(conn, tid)
    assert _park_without_probe(conn, tid) == [episode.EVIDENCE_EVENT, "scheduled"]
    assert _episode(conn, tid) is None


def test_delivered_but_unreconciled_foreign_probe_is_cleared(kanban_home):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)  # this home never parks with a probe
        peer = carry.replica(conn)
        try:
            assert kb.schedule_task(peer, tid, reason="waiting", wake_check="exit 1")
            state = wc.get_state(peer, tid)
            assert state is not None
            wc.apply_result(peer, tid, state["cmd"], 1, "", generation=state["generation"],
                            now=int(time.time()))
            assert _episode(peer, tid)
            carry.import_events(peer, conn, tid)  # delivered, not yet reconciled here
            before = len(_kinds(conn, tid))
            assert kb.schedule_task(conn, tid, reason=SOAK, clock_only=True)
            added = [k for k in _kinds(conn, tid)[before:] if k != "commented"]
            assert added == [episode.EVIDENCE_EVENT, "scheduled"]
            carry.import_events(conn, peer, tid)
            episode.reconcile(peer, tid)
            assert _episode(peer, tid) is None
        finally:
            peer.close()


def test_outside_probe_less_park_keeps_exit_reset_for_unobserved_episode(parked):
    conn, tid, _ = parked
    home = carry.replica(conn)
    try:
        home.execute("DELETE FROM task_wake_episode_cmd WHERE task_id = ?", (tid,))  # home knows no probe
        base.result(parked, 0)
        home.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
        before = len(_kinds(home, tid))
        kb._append_event(home, tid, "scheduled", {"reason": "late"})
        rows = home.execute("SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
                            (tid,)).fetchall()[before:]
        assert [r[0] for r in rows] == [episode.EVIDENCE_EVENT, "scheduled"]
        assert json.loads(rows[1][1]) == {"reason": "late"}
        carry.import_events(home, conn, tid)
        episode.reconcile(conn, tid)
        state = wc.get_state(conn, tid)
        assert state is not None and state["episode_first_at"] is None
    finally:
        home.close()
