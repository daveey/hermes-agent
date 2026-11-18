"""Replica-order-independent containment episodes; no lifecycle or authority shortcuts."""
from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from tests.hermes_cli import test_kanban_containment_legacy_paths as legacy

board = legacy.board

TARGET = {"gateway": "fixture-home", "board": "fixture", "task_id": "t_0123456789abcdef"}
AUTHORITY = {**TARGET, "task_id": "t_1123456789abcdef"}
PRODUCER = {**TARGET, "task_id": "t_2123456789abcdef"}
STOP_UID = "019a98b4-0000-7000-8000-000000000001"


def stop(uid=STOP_UID):
    return {"uid": uid, "kind": "contained", "task_id": TARGET["task_id"], "payload": {
        "version": 1, "target": TARGET, "policy": {"version": 1, "authority": AUTHORITY},
        "producer_baseline": None,
    }}


def test_valid_episode_survives_restart_and_event_reordering():
    from hermes_cli.kanban_containment_state import reduce_events

    first, second = stop(), stop("019a98b4-0000-7000-8000-000000000002")
    expected = (first["uid"], second["uid"])
    for events in ([first, second], [second, first], [second, first, second],
                   json.loads(json.dumps([first, second]))):
        hold = reduce_events(events)
        assert hold.episodes == expected
        assert hold.unresolved is False
    assert reduce_events([]) is None
    assert reduce_events([{"kind": "unblocked"}]) is None



def release(episode, *, kind="authority"):
    from hermes_cli.kanban_containment_state import fingerprint

    evidence = {"kind": kind, kind: AUTHORITY if kind == "authority" else PRODUCER,
                "run_uid": "019a98b4-0000-7000-8000-000000000099"}
    if kind == "producer":
        evidence["event_uid"] = "019a98b4-0000-7000-8000-000000000098"
    return {"uid": "019a98b4-0000-7000-8000-000000000003", "kind": "containment_released",
            "task_id": TARGET["task_id"], "payload": {"version": 1, "episode": episode["uid"],
                "containment_sha256": fingerprint(episode["payload"]), "resume_status": "ready",
                "evidence": evidence}}


def test_release_needs_verified_episode_bound_receipt_and_only_releases_its_episode():
    from hermes_cli.kanban_containment_state import reduce_events

    first, second = stop(), stop("019a98b4-0000-7000-8000-000000000002")
    receipt = release(first)
    assert reduce_events([first, receipt]).episodes == (first["uid"],)
    assert reduce_events([first, receipt], verify_release=lambda *_: False).episodes == (first["uid"],)
    assert reduce_events([first, receipt], verify_release=lambda *_: 1).episodes == (first["uid"],)
    for events in ([first, receipt], [receipt, first], json.loads(json.dumps([receipt, first]))):
        seen = []
        def verified(event, episode):
            seen.append((event["uid"], episode["uid"]))
            return True
        assert reduce_events(events, verify_release=verified) is None
        assert seen == [(receipt["uid"], first["uid"])]
    assert reduce_events([first, receipt, second], verify_release=lambda *_: True).episodes == (second["uid"],)


@pytest.mark.parametrize("field,value", [
    ("version", True), ("version", 2), ("episode", "019a98b4-0000-7000-8000-000000000002"),
    ("containment_sha256", "0" * 64), ("resume_status", "review"), ("extra", 1),
    ("evidence", {}), ("evidence", {"kind": "timer"}),
])
def test_malformed_or_stale_release_cannot_reach_verifier(field, value):
    from hermes_cli.kanban_containment_state import reduce_events

    first = stop()
    receipt = release(first)
    receipt["payload"][field] = value
    def forbidden(*_):
        pytest.fail("invalid receipt must not reach authority verifier")
    assert reduce_events([first, receipt], verify_release=forbidden).episodes == (first["uid"],)


@pytest.mark.parametrize("change", [
    lambda e: e.update(uid=None), lambda e: e.update(uid=STOP_UID.upper()),
    lambda e: e.update(uid=STOP_UID.replace("-7000-", "-4000-")),
    lambda e: e.update(payload="not json"), lambda e: e.update(payload="[]"),
    lambda e: e["payload"].update(version=True), lambda e: e["payload"].update(version=2),
    lambda e: e["payload"].update(extra=1), lambda e: e["payload"].update(target={}),
    lambda e: e["payload"]["policy"].update(authority=TARGET),
    lambda e: e.update(task_id=AUTHORITY["task_id"]),
    lambda e: e["payload"].update(producer_baseline={"event_uid": STOP_UID, "run_uid": STOP_UID}),
])
def test_malformed_containment_is_never_erased_by_a_release(change):
    from hermes_cli.kanban_containment_state import reduce_events

    first = stop()
    receipt = release(first)
    change(first)
    hold = reduce_events([receipt, first], verify_release=lambda *_: True)
    assert hold is not None and hold.unresolved is True


@pytest.mark.parametrize("change", [
    lambda e: e.update(uid=None), lambda e: e.update(task_id=AUTHORITY["task_id"]),
    lambda e: e["payload"].update(evidence=[]),
    lambda e: e["payload"]["evidence"].update(run_uid=42),
    lambda e: e["payload"]["evidence"].update(extra=True),
    lambda e: e["payload"]["evidence"].update(authority={}),
    lambda e: e["payload"]["evidence"]["authority"].update(task_id=PRODUCER["task_id"]),
    lambda e: e["payload"]["evidence"]["authority"].update(board="other"),
])
def test_release_identity_must_match_scope_before_verification(change):
    from hermes_cli.kanban_containment_state import reduce_events

    first = stop()
    receipt = copy.deepcopy(release(first))
    change(receipt)
    def forbidden(*_):
        pytest.fail("invalid release identity reached verifier")
    assert reduce_events([first, receipt], verify_release=forbidden) is not None


def test_new_producer_proof_is_distinct_from_the_frozen_event_and_run():
    from hermes_cli.kanban_containment_state import reduce_events

    first = stop()
    first["payload"]["policy"]["producer"] = PRODUCER
    first["payload"]["producer_baseline"] = {"run_uid": STOP_UID, "event_uid": STOP_UID}
    receipt = release(first, kind="producer")
    assert reduce_events([receipt, first], verify_release=lambda *_: True) is None
    for key in ("run_uid", "event_uid"):
        old = copy.deepcopy(receipt)
        old["payload"]["evidence"][key] = STOP_UID
        assert reduce_events([first, old], verify_release=lambda *_: True) is not None
    no_producer = stop()
    assert reduce_events([no_producer, release(no_producer, kind="producer")],
                         verify_release=lambda *_: True) is not None


def _guarded(board):
    from hermes_cli import kanban_containment_guard as guard
    from hermes_cli import kanban_db as kb

    relay, consumer = legacy._relay(board)
    if "uid" not in {r[1] for r in board.execute("PRAGMA table_info(task_events)")}:
        board.execute("ALTER TABLE task_events ADD COLUMN uid TEXT")
    guard.install(board)
    episode = copy.deepcopy(stop())
    episode["task_id"] = relay
    episode["payload"]["target"]["task_id"] = relay
    with kb.write_txn(board):
        with guard.authorized_write(board, relay):
            kb._append_event(board, relay, "contained", episode["payload"])
            board.execute("UPDATE task_events SET uid=? WHERE task_id=? AND kind='contained'", (STOP_UID, relay))
            board.execute("UPDATE tasks SET status='triage',current_run_id=NULL WHERE id=?", (relay,))
    assert guard.pending(board, relay)
    return relay, consumer


@pytest.mark.parametrize("sql", [
    "DELETE FROM tasks WHERE id=?", "UPDATE tasks SET status='review' WHERE id=?",
    "UPDATE tasks SET current_run_id=999 WHERE id=?", "UPDATE tasks SET completed_at=1 WHERE id=?",
    "DELETE FROM task_events WHERE task_id=? AND kind='contained'",
    "UPDATE task_events SET kind='ignored' WHERE task_id=? AND kind='contained'",
    "UPDATE task_events SET payload='{}' WHERE task_id=? AND kind='contained'",
    "UPDATE task_events SET uid='changed' WHERE task_id=? AND kind='contained'",
    "DELETE FROM task_links WHERE parent_id=?",
    "INSERT INTO task_links (parent_id,child_id) SELECT id,id FROM tasks WHERE id=?",
    "UPDATE task_links SET parent_id=child_id WHERE parent_id=?",
])
def test_backstop_blocks_status_history_and_edge_mutation(board, sql):
    from hermes_cli import kanban_db as kb

    relay, _ = _guarded(board)
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        with kb.write_txn(board):
            board.execute(sql, (relay,))


def test_writer_permit_is_transaction_connection_and_task_scoped(board):
    from hermes_cli import kanban_containment_guard as guard
    from hermes_cli import kanban_db as kb

    relay, consumer = _guarded(board)
    with pytest.raises(ValueError, match="active transaction"):
        with guard.authorized_write(board, relay):
            pass
    with kb.write_txn(board):
        for granted in (relay, consumer):
            with guard.authorized_write(board, granted):
                for status in ("done", "archived", "ready", "review", "todo"):
                    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
                        board.execute("UPDATE tasks SET status=? WHERE id=?", (status, relay))
    # A new connection registers its own functions against the durable triggers.
    path = board.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(path, isolation_level=None) as peer:
        guard.install(peer)
        assert guard.pending(peer, relay)
        with kb.write_txn(board):
            with guard.authorized_write(board, relay):
                assert peer.execute("SELECT kanban_containment_write(?)", (relay,)).fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            peer.execute("UPDATE tasks SET status='ready' WHERE id=?", (relay,))
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET status='ready' WHERE id=?", (relay,))


def test_protected_receipt_insert_needs_exact_task_transaction_permit(board):
    from hermes_cli import kanban_containment_guard as guard
    from hermes_cli import kanban_db as kb

    relay, consumer = _guarded(board)
    for kind in ("contained", "containment_released"):
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            with kb.write_txn(board):
                kb._append_event(board, consumer, kind, {})
        with kb.write_txn(board):
            with guard.authorized_write(board, relay):
                with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
                    kb._append_event(board, consumer, kind, {})
    # Permits may not destroy evidence or dependencies, even for the named task.
    with kb.write_txn(board):
        with guard.authorized_write(board, relay):
            for sql in ("DELETE FROM task_events WHERE task_id=? AND kind='contained'",
                        "DELETE FROM task_links WHERE parent_id=?"):
                with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
                    board.execute(sql, (relay,))


def test_conflicting_same_uid_containment_never_releases(board):
    from hermes_cli.kanban_containment_state import reduce_events

    first = stop()
    conflict = copy.deepcopy(first)
    conflict["payload"]["target"]["gateway"] = "different-home"
    for events in ([first, conflict], [conflict, first]):
        hold = reduce_events([*events, release(first), release(conflict)], verify_release=lambda *_: True)
        assert hold is not None and hold.unresolved is True


def test_local_backstop_preserves_edges_and_refuses_generic_completion(board):
    from hermes_cli import kanban_containment_guard as guard
    from hermes_cli import kanban_db as kb
    import sqlite3

    relay, consumer = legacy._relay(board)
    guard.install(board)
    episode = stop()
    episode["task_id"] = relay
    episode["payload"]["target"] = {**TARGET, "task_id": relay}
    with kb.write_txn(board):
        with guard.authorized_write(board, relay):
            kb._append_event(board, relay, "contained", episode["payload"])
            board.execute("UPDATE tasks SET status='triage',current_run_id=NULL WHERE id=?", (relay,))
    before = legacy._edges(board)
    with kb.write_txn(board):
        with guard.authorized_write(board, relay):
            with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
                board.execute("UPDATE tasks SET status='done' WHERE id=?", (relay,))
    assert kb.complete_task(board, relay, summary="Must not release dependency") is False
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET status='done' WHERE id=?", (relay,))
    assert kb.get_task(board, relay).status == "triage"
    assert kb.get_task(board, consumer).status == "todo"
    assert legacy._edges(board) == before
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        with kb.write_txn(board):
            board.execute("DELETE FROM task_links WHERE parent_id=?", (relay,))
    assert legacy._edges(board) == before


def test_containment_is_not_released_by_generic_lifecycle_events():
    from hermes_cli.kanban_containment_state import reduce_events

    events = [{"uid": "019a98b4-0000-7000-8000-000000000001", "kind": "contained",
               "payload": json.dumps({"version": 1}), "task_id": "t_0123456789abcdef"}]
    events.extend({"kind": kind, "payload": "{}"} for kind in (
        "unblocked", "promoted_manual", "reopened", "completed", "review_requested", "archived"))
    hold = reduce_events(events)
    assert hold is not None
    assert hold.unresolved is True
    assert hold.episodes == ()
