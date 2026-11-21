"""Terminal-closure recovery, reducer and quarantine behaviour.

A closed episode (``containment_closed``) is terminal: no release may discharge it, no recovery
path may mistake it for a parked card, and a quarantined closure page must be held, restored and
released exactly like a stop or release receipt. Each test drives the public/behavioural seam with
real fixtures and asserts the outcome, so removing the ``containment_closed`` handling fails it.
"""
import copy
import json

import pytest

from hermes_cli import kanban_containment_connections as connections
from hermes_cli import kanban_containment_guard as guard
from hermes_cli import kanban_containment_lifecycle as life
from hermes_cli import kanban_containment_replica as replica
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_quarantine as quarantine
from hermes_cli import kanban_sync_quarantine_release as qr
from hermes_cli.kanban_containment_state import _payload, _stop, fingerprint
from tests.hermes_cli import test_kanban_containment_lifecycle as lifecycle
from tests.hermes_cli import test_kanban_containment_replica as fixtures
from tests.hermes_cli import test_kanban_containment_terminal_replica as tr

board = lifecycle.board
kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


def _closed_local(conn):
    """A locally closed card: authority archived first, then the held card archived (terminal)."""
    relay, consumer, authority, target, spec = lifecycle.setup(conn)
    episode = lifecycle.contain(conn, relay, target, spec)["episode"]
    authority_run = lifecycle.run_uid(conn, authority)
    assert kb.archive_task(conn, authority)
    return relay, consumer, authority, target, spec, episode, authority_run


def _close(conn, relay):
    assert kb.archive_task(conn, relay)
    assert kb.get_task(conn, relay).status == "archived"


# ---------------------------------------------------------------- M11 closed() pending request

def test_closed_is_false_while_a_stop_request_is_unresolved(synced_conn):
    conn = synced_conn
    target, _, stop, close, hub = tr.scenario(conn)
    marker = sync.RemoteChange(1, "tasks", target, "upsert",
                              {"status": "archived", "containment_episode": stop["event_uid"]}, 100, "", "home")
    fixtures.apply(conn, hub, [marker, fixtures.event(stop, hub, 2), fixtures.event(close, hub, 3)])
    from hermes_cli.kanban_containment_terminal import closed
    assert closed(conn, target) is True
    from hermes_cli import kanban_containment_canonical as canonical
    canonical._schema(conn)
    state = sync.sync_state(conn)
    unknown = sync.uuid7()
    conn.execute("INSERT INTO containment_delivery(request_uid,board,gateway,op_json,confirmed) VALUES (?,?,?,?,0)",
                 (unknown, state["board_id"], state["gateway_id"],
                  json.dumps({"op": "contain", "task": target, "event_uid": unknown})))
    conn.commit()
    assert not closed(conn, target)
    # The authenticated closure is still on the card; only the unresolved request withdraws "closed".
    assert kb.get_task(conn, target).status == "archived"
    assert connections.held(conn, target)


# ---------------------------------------------------------------- M17 reducer keeps closure authoritative

def test_forged_release_cannot_discharge_a_closed_episode_in_the_contained_wait_reducer(board, monkeypatch):
    from hermes_cli import kanban_contained_wait as wait
    relay, _, _authority, _target, spec, episode, _ = _closed_local(board)
    _close(board, relay)
    stop = dict(board.execute("SELECT * FROM task_events WHERE uid=?", (episode,)).fetchone())
    frozen = _stop(stop)
    receipt = {"version": 1, "episode": episode, "containment_sha256": fingerprint(frozen),
               "resume_status": "ready",
               "evidence": {"kind": "authority", "authority": spec["authority"], "run_uid": sync.uuid7()}}
    with kb.write_txn(board), guard.authorized_write(board, relay):
        life._append(board, relay, "containment_released", receipt)
    # Even an accepting verifier must not let a release discharge an authenticated closure.
    monkeypatch.setattr(connections, "verifier", lambda _conn, _event, _stop_event: True)
    found = wait._active_episode(board, relay, episode)
    assert found is not None and found["uid"] == episode


# ---------------------------------------------------------------- M18 resume status from events

def test_closure_is_the_newest_lifecycle_event_for_resume_status(board):
    relay, _, authority, target, spec = lifecycle.setup(board)
    lifecycle.contain(board, relay, target, spec)
    with kb.write_txn(board):
        kb._append_event(board, relay, "blocked", {"resume_status": "review"})
    assert kb._resume_status_from_events(board, relay) == "review"
    assert kb.archive_task(board, authority)
    assert kb.archive_task(board, relay)
    assert board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='containment_closed'",
                         (relay,)).fetchone()
    # The closure is newer than the review park, so it, not the stale park, decides the status.
    assert kb._resume_status_from_events(board, relay) == "ready"


# ---------------------------------------------------------------- M19 supervisor resume lifecycle kinds

def test_resume_exhaustion_refusal_sees_a_closure_as_the_newest_lifecycle_event(board):
    from hermes_cli.kanban_resume_exec import exhaustion_refusal
    task = kb.create_task(board, title="Parked then closed", assignee="default", triage=True)
    with kb.write_txn(board):
        kb._append_event(board, task, "blocked", {"rekind_reason": "continuation_exhausted"})
    with kb.write_txn(board), guard.authorized_write(board, task):
        kb._append_event(board, task, "containment_closed", {})
    refusal = exhaustion_refusal(board, task)
    assert refusal is not None
    assert "newest lifecycle event: containment_closed" in refusal


# ---------------------------------------------------------------- M20 resume_local terminal refusal

def test_resume_local_refuses_a_closed_episode_with_the_terminal_diagnostic(board):
    # On a local board the later triage-status check also refuses an archived card, so this
    # fence is partly redundant; the exact terminal diagnostic is the observable contract.
    relay, _, _authority, target, _spec, episode, _ = _closed_local(board)
    _close(board, relay)
    # A live claimed actor passes the actor fence, so the terminal fence is the first refusal.
    actor = kb.create_task(board, title="Live actor", assignee="default")
    assert kb.claim_task(board, actor, claimer="default") is not None
    board.execute("UPDATE task_runs SET uid=? WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",
                  (sync.uuid7(), actor))
    actor_run = lifecycle.run_uid(board, actor)
    before = board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='containment_released'",
                           (relay,)).fetchone()[0]
    with pytest.raises(life.ContainmentRefused, match="terminal containment cannot resume"):
        life.resume_local(board, target=target, episode=episode, actor_task=actor,
                          actor_run_uid=actor_run, kind="authority")
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='containment_released'",
                         (relay,)).fetchone()[0] == before
    assert connections.held(board, relay)
    assert kb.get_task(board, relay).status == "archived"


# ---------------------------------------------------------------- M30 / M31 / M32 quarantine

def _quarantined_closure(conn):
    """The Board re-stamps the stop receipt after this host cached it; the closure page follows."""
    target, child, stop, close, hub = tr.scenario(conn)
    uid = stop["event_uid"]
    replica.remember(conn, hub.events[uid], board="fixture", task=target, uid=uid)
    hub.events[uid]["row"]["created_at"] += 1
    marker = sync.RemoteChange(3, "tasks", target, "upsert",
                              {"status": "archived", "containment_episode": uid}, 100, "", "home")
    page = [fixtures.event(stop, hub, 1), fixtures.event(close, hub, 2), marker]
    assert quarantine.certified_page(conn, hub, "fixture", page) == []
    return target, child, stop, close, hub


def test_closure_receipt_is_quarantined_and_withheld_like_any_protected_receipt(synced_conn):
    conn = synced_conn
    target, _child, stop, close, _hub = _quarantined_closure(conn)
    # The closure page is itself a protected receipt, journalled under its own uid.
    assert {e["receipt"] for e in quarantine.entries(conn)} == {stop["event_uid"], close["event_uid"]}
    assert quarantine.held(conn, target)
    assert kb.claim_task(conn, target) is None
    assert kb.get_task(conn, target).status != "archived"
    assert not conn.execute("SELECT 1 FROM task_events WHERE uid=?", (close["event_uid"],)).fetchone()


def test_rebuild_restores_closure_receipts_with_their_stop_receipt(synced_conn, tmp_path):
    conn = synced_conn
    target, _, stop, close, hub = tr.scenario(conn)
    marker = sync.RemoteChange(1, "tasks", target, "upsert",
                              {"status": "archived", "containment_episode": stop["event_uid"]}, 100, "", "home")
    fixtures.apply(conn, hub, [marker, fixtures.event(stop, hub, 2), fixtures.event(close, hub, 3)])
    assert kb.get_task(conn, target).status == "archived"
    original = copy.deepcopy(hub.events[close["event_uid"]])
    quarantine.install(conn)
    data = quarantine.snapshot(conn)
    assert any(json.loads(raw)["row"]["kind"] == "containment_closed" for raw in data["receipts"])
    from hermes_cli import kanban_db_connect
    with kanban_db_connect.connect_closing(db_path=tmp_path / "rebuilt.db") as fresh:
        sync.set_identity(fresh, "fixture", "worker")
        # M31: the closure's stop receipt is found while re-validating the preserved cache.
        quarantine.restore(fresh, data)
        assert replica._cached(fresh, close["event_uid"]) == original
        assert replica._cached(fresh, stop["event_uid"]) is not None


def test_quarantined_closure_is_released_through_its_stop_and_certified_chain(synced_conn):
    conn = synced_conn
    target, child, stop, close, hub = _quarantined_closure(conn)
    # M32: the release resolves the closure's episode, so both receipts are re-certified together.
    for uid in (stop["event_uid"], close["event_uid"]):
        qr.release(conn, hub, "fixture", uid, reason="Board re-stamp ruled benign", operator="test")
    assert quarantine.entries(conn) == []
    assert not quarantine.held(conn, target)
    assert conn.execute("SELECT kind FROM task_events WHERE uid=?", (close["event_uid"],)).fetchone()[0] \
        == "containment_closed"
    assert conn.execute("SELECT kind FROM task_events WHERE uid=?", (stop["event_uid"],)).fetchone()[0] == "contained"
    assert connections.held(conn, target)
    assert kb.claim_task(conn, target) is None
    assert kb.get_task(conn, child).status == "todo"
