"""Probe-less parks add evidence only when some probe could still need clearing."""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as wc
from hermes_cli import kanban_wake_episode as episode
from tests.hermes_cli import test_kanban_wake_check as helpers
from tests.hermes_cli import test_kanban_wake_episode as base
from tests.hermes_cli import test_kanban_wake_episode_carry as carry

kanban_home = base.kanban_home
parked = base.parked
COOLDOWN = "SCHEDULED_UNTIL=2026-09-13T00:00:00Z rate-limit cooldown"


def _kinds(conn, tid):
    return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]


def _park_without_probe(conn, tid):
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    before = len(_kinds(conn, tid))
    assert kb.schedule_task(conn, tid, reason=COOLDOWN, clock_only=True)
    return [k for k in _kinds(conn, tid)[before:] if k not in {"claimed", "commented"}]


def _episode(conn, tid):
    return conn.execute("SELECT cmd FROM task_wake_episodes WHERE task_id = ?", (tid,)).fetchone()


def test_first_park_of_created_runnable_card_carries_a_reset(kanban_home):
    # A runnable card may reach a peer and be parked on a probe there before this home's
    # first park, so local creation alone proves nothing: the first park carries a reset.
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        before = len(_kinds(conn, tid))
        assert kb.schedule_task(conn, tid, reason=COOLDOWN, clock_only=True)
        assert [k for k in _kinds(conn, tid)[before:] if k != "commented"] == [episode.EVIDENCE_EVENT, "scheduled"]


def _triage(conn):
    tid = kb.create_task(conn, title="t", assignee="worker", triage=True)
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "triage"
    return tid


def test_direct_triage_park_of_local_triage_card_adds_no_evidence(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _triage(conn)
        before = len(_kinds(conn, tid))
        assert kb.schedule_task(conn, tid, reason=COOLDOWN, clock_only=True)
        assert [k for k in _kinds(conn, tid)[before:] if k != "commented"] == ["scheduled"]
        assert _park(conn, tid, "scheduled") == [episode.EVIDENCE_EVENT, "scheduled"]  # marker consumed


@pytest.mark.parametrize("route", [("todo", "ready"), ("blocked",)])
def test_triage_card_leaving_triage_otherwise_is_not_fresh(kanban_home, route):
    # Raw status writes, as a synced remote transition applies them.
    with kbc.connect_closing() as conn:
        tid = _triage(conn)
        for status in route:
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
        assert _park(conn, tid, "scheduled") == [episode.EVIDENCE_EVENT, "scheduled"]


@pytest.mark.parametrize("status", ["running", "ready"])
def test_triage_card_entering_corridor_is_not_fresh_without_a_further_transition(kanban_home, status):
    # One synced remote transition out of triage, then a park with no further status write.
    with kbc.connect_closing() as conn:
        tid = _triage(conn)
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
        before = len(_kinds(conn, tid))
        kb._append_event(conn, tid, "continued", {"reason": "rate-limit cooldown"})
        assert _kinds(conn, tid)[before:] == [episode.EVIDENCE_EVENT, "continued"]


def test_created_runnable_card_first_park_without_status_write_resets_foreign_episode(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")  # no home status write before its park
        assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] in episode._CORRIDOR
        peer = carry.replica(conn)
        try:
            t0 = int(time.time())
            assert tuple(_peer_episode(peer, tid, t0)) == (t0, 1)  # foreign P, never delivered to home
            before = len(_kinds(conn, tid))
            kb._append_event(conn, tid, "continued", {"reason": "rate-limit cooldown"})
            assert _kinds(conn, tid)[before:] == [episode.EVIDENCE_EVENT, "continued"]
            carry.import_events(conn, peer, tid)
            episode.reconcile(peer, tid)
            assert _episode(peer, tid) is None
        finally:
            peer.close()


def test_probe_park_of_triage_card_drops_freshness(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _triage(conn)
        assert kb.schedule_task(conn, tid, reason="waiting", wake_check="exit 1")  # direct triage park, probe P
        peer = carry.replica(conn)
        try:
            assert _park(peer, tid, "scheduled") == [episode.EVIDENCE_EVENT, "scheduled"]  # peer clears P
            carry.import_events(peer, conn, tid)  # delivered; the peer's status write is still delayed
            before = len(_kinds(conn, tid))
            kb._append_event(conn, tid, "continued", {"reason": "rate-limit cooldown"})
            assert _kinds(conn, tid)[before:] == [episode.EVIDENCE_EVENT, "continued"]
        finally:
            peer.close()


@pytest.mark.parametrize("kind", ["scheduled", "continued"])
def test_first_home_park_resets_undelivered_foreign_episode_on_created_card(kanban_home, kind):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)  # created here, runnable: the peer receives it
        peer = carry.replica(conn)
        try:
            t0 = int(time.time())
            assert tuple(_peer_episode(peer, tid, t0)) == (t0, 1)  # foreign P, never delivered to home
            assert _park(conn, tid, kind) == [episode.EVIDENCE_EVENT, kind]  # home's first, no-probe park
            carry.import_events(conn, peer, tid)
            episode.reconcile(peer, tid)
            assert _episode(peer, tid) is None
            t1 = t0 + 3600
            assert tuple(_peer_episode(peer, tid, t1)) == (t1, 1)  # same probe: old age/count gone
        finally:
            peer.close()


def test_every_probe_less_repark_carries_a_reset(parked):
    conn, tid, _ = parked
    # A carried reset proves nothing about peers afterwards: each re-park repeats it.
    for _ in range(3):
        assert _park_without_probe(conn, tid) == [episode.EVIDENCE_EVENT, "scheduled"]


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
            assert kb.schedule_task(conn, tid, reason=COOLDOWN, clock_only=True)
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


def _peer_episode(peer, tid, now):
    if peer.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] != "running":
        peer.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(peer, tid, claimer="peer") is not None
    assert kb.schedule_task(peer, tid, reason="waiting", wake_check="exit 1")
    state = wc.get_state(peer, tid)
    wc.apply_result(peer, tid, state["cmd"], 1, "", generation=state["generation"], now=now)
    return peer.execute("SELECT first_at, checks FROM task_wake_episodes WHERE task_id = ?", (tid,)).fetchone()


@pytest.mark.parametrize("kind", ["scheduled", "continued"])
def test_in_corridor_park_resets_undelivered_foreign_episode(kanban_home, kind):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        before = len(_kinds(conn, tid))
        assert kb.schedule_task(conn, tid, reason=COOLDOWN, clock_only=True)
        assert [k for k in _kinds(conn, tid)[before:] if k != "commented"] == [episode.EVIDENCE_EVENT, "scheduled"]
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        peer = carry.replica(conn)
        try:
            t0 = int(time.time())
            assert tuple(_peer_episode(peer, tid, t0)) == (t0, 1)  # never delivered to home
            before = len(_kinds(conn, tid))
            if kind == "scheduled":
                assert kb.schedule_task(conn, tid, reason=COOLDOWN, clock_only=True)
            else:
                kb._append_event(conn, tid, "continued", {"reason": "rate-limit cooldown"})
            added = [k for k in _kinds(conn, tid)[before:] if k != "commented"]
            assert added == [episode.EVIDENCE_EVENT, kind]
            carry.import_events(conn, peer, tid)
            episode.reconcile(peer, tid)
            t1 = t0 + 3600
            assert tuple(_peer_episode(peer, tid, t1)) == (t1, 1)  # same probe: old age/count gone
        finally:
            peer.close()


def _park(conn, tid, kind):
    if conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] != "running":
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
    before = len(_kinds(conn, tid))
    if kind == "scheduled":
        assert kb.schedule_task(conn, tid, reason=COOLDOWN, clock_only=True)
    else:
        kb._append_event(conn, tid, "continued", {"reason": "rate-limit cooldown"})
    return [k for k in _kinds(conn, tid)[before:] if k not in {"claimed", "commented"}]


@pytest.mark.parametrize("kind", ["scheduled", "continued"])
def test_park_after_explicit_reset_resets_new_undelivered_foreign_episode(kanban_home, kind):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        assert _park(conn, tid, kind) == [episode.EVIDENCE_EVENT, kind]  # runnable card: never fresh
        peer = carry.replica(conn)
        try:
            t0 = int(time.time())
            assert tuple(_peer_episode(peer, tid, t0)) == (t0, 1)  # undelivered to home
            assert _park(conn, tid, kind) == [episode.EVIDENCE_EVENT, kind]  # explicit reset
            carry.import_events(conn, peer, tid)
            episode.reconcile(peer, tid)
            t1 = t0 + 3600
            assert tuple(_peer_episode(peer, tid, t1)) == (t1, 1)  # new foreign P, still undelivered
            assert _park(conn, tid, kind) == [episode.EVIDENCE_EVENT, kind]  # third park: reset again
            assert _episode(peer, tid)
            carry.import_events(conn, peer, tid)
            episode.reconcile(peer, tid)
            # The second foreign episode's age/count are gone too (a further peer park here
            # would trip the unrelated park-cycle guard, so read the episode row directly).
            assert peer.execute("SELECT 1 FROM task_wake_episodes WHERE task_id = ?", (tid,)).fetchone() is None
        finally:
            peer.close()


def test_sync_imported_task_is_not_fresh(kanban_home):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        conn.execute("DELETE FROM task_wake_episode_fresh WHERE task_id = ?", (tid,))
        row = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
        conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
        conn.execute(f"INSERT INTO tasks ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     tuple(row.values()))  # raw row insert, as sync import does: no local 'created'
        assert conn.execute("SELECT 1 FROM task_wake_episode_fresh WHERE task_id = ?", (tid,)).fetchone() is None
        assert _park(conn, tid, "scheduled") == [episode.EVIDENCE_EVENT, "scheduled"]


def test_sync_imported_corridor_entry_is_not_fresh(kanban_home):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        conn.execute("DELETE FROM task_wake_episode_fresh WHERE task_id = ?", (tid,))
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))  # raw status write
        assert _park(conn, tid, "continued") == [episode.EVIDENCE_EVENT, "continued"]
