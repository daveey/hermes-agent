"""Triage freshness may skip park evidence only on a genuinely unsynced board.

On a synced board a peer can promote/claim/park (or directly park) a triage card on a
probe while this home still sees it as triage; the home's direct triage -> scheduled
park must then carry the reset, or the delayed foreign episode keeps its age/count.
It carries it inline, so the atomic park still appends one event (test_kanban_triage_park).
"""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_wake_check as wc
from hermes_cli import kanban_wake_episode as episode
from tests.hermes_cli import test_kanban_wake_episode as base
from tests.hermes_cli import test_kanban_wake_episode_carry as carry

kanban_home = base.kanban_home
COOLDOWN = "SCHEDULED_UNTIL=2026-09-13T00:00:00Z rate-limit cooldown"


def _enable_sync(monkeypatch):
    monkeypatch.setattr(sync, "sync_config", lambda: {"enabled": True})
    kb.init_db()  # re-runs install_or_remove: the board now has durable sync state


def _triage(conn):
    tid = kb.create_task(conn, title="t", assignee="worker", triage=True)
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "triage"
    return tid


def _fresh(conn, tid):
    return conn.execute("SELECT 1 FROM task_wake_episode_fresh WHERE task_id = ?", (tid,)).fetchone() is not None


def _home_park_kinds(conn, tid):
    before = len(_kinds(conn, tid))
    assert kb.schedule_task(conn, tid, reason=COOLDOWN, clock_only=True)  # home still sees triage
    return [k for k in _kinds(conn, tid)[before:] if k != "commented"]


def _kinds(conn, tid):
    return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]


def _inline_reset(conn, tid):
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'scheduled' "
                       "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    return episode._SNAPSHOT in json.loads(row[0] or "{}")


def _exit1(peer, tid, now):
    state = wc.get_state(peer, tid)
    wc.apply_result(peer, tid, state["cmd"], 1, "", generation=state["generation"], now=now)
    return tuple(peer.execute("SELECT first_at, checks FROM task_wake_episodes WHERE task_id = ?",
                              (tid,)).fetchone())


def _peer_promote_claim_park(peer, tid):
    assert kb.promote_task(peer, tid, actor="peer")[0]
    assert kb.claim_task(peer, tid, claimer="peer") is not None
    assert kb.schedule_task(peer, tid, reason="waiting", wake_check="exit 1")


def _peer_direct_park(peer, tid):
    assert kb.schedule_task(peer, tid, reason="waiting", wake_check="exit 1")  # straight from triage


@pytest.mark.parametrize("peer_park", [_peer_promote_claim_park, _peer_direct_park])
def test_delayed_foreign_triage_park_is_reset_by_home_triage_park(kanban_home, monkeypatch, peer_park):
    _enable_sync(monkeypatch)
    with kbc.connect_closing() as conn:
        tid = _triage(conn)
        peer = carry.replica(conn)
        try:
            peer_park(peer, tid)
            t0 = int(time.time())
            assert _exit1(peer, tid, t0) == (t0, 1)  # foreign episode, not yet delivered home
            assert _home_park_kinds(conn, tid) == ["scheduled"]  # atomic: the reset rides inline
            assert _inline_reset(conn, tid)
            carry.import_events(peer, conn, tid)  # the peer's pre-reset episode reaches home late
            episode.reconcile(conn, tid)
            assert conn.execute("SELECT 1 FROM task_wake_episodes WHERE task_id = ?", (tid,)).fetchone() is None
            carry.import_events(conn, peer, tid)  # home's park reaches the peer
            episode.reconcile(peer, tid)
            assert peer.execute("SELECT 1 FROM task_wake_episodes WHERE task_id = ?", (tid,)).fetchone() is None
            peer.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            assert kb.claim_task(peer, tid, claimer="peer") is not None
            assert kb.schedule_task(peer, tid, reason="waiting", wake_check="exit 1")  # same probe P
            assert _exit1(peer, tid, t0 + 3600) == (t0 + 3600, 1)  # old age/count gone
        finally:
            peer.close()


def test_synced_at_create_triage_park_is_one_event_carrying_the_reset(kanban_home, monkeypatch):
    _enable_sync(monkeypatch)
    with kbc.connect_closing() as conn:
        tid = _triage(conn)
        assert _home_park_kinds(conn, tid) == ["scheduled"]
        assert _inline_reset(conn, tid)
        assert not _fresh(conn, tid)  # consumed: the next park carries the separate reset


def test_enabling_sync_invalidates_markers_of_cards_created_unsynced(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = _triage(conn)
        assert _fresh(conn, tid)  # unsynced: the optimization applies
    _enable_sync(monkeypatch)
    with kbc.connect_closing() as conn:
        assert not _fresh(conn, tid)  # cleared in the enabling transaction
        assert _home_park_kinds(conn, tid) == [episode.EVIDENCE_EVENT, "scheduled"]


def test_unsynced_triage_card_keeps_the_optimization(kanban_home):
    with kbc.connect_closing() as conn:
        assert sync.sync_state(conn) is None
        tid = _triage(conn)
        assert _fresh(conn, tid)
        assert _home_park_kinds(conn, tid) == ["scheduled"]
        assert not _inline_reset(conn, tid)  # nothing to clear: no evidence at all
