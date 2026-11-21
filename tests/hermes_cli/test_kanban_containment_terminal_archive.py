"""Terminal containment archive is not release or dependency success."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_containment_connections as connections
from tests.hermes_cli import test_kanban_containment_lifecycle as lifecycle

board = lifecycle.board


def test_archive_closed_authority_retains_hold_and_dependency(board):
    relay, consumer, authority, target, spec = lifecycle.setup(board)
    episode = lifecycle.contain(board, relay, target, spec)["episode"]
    assert kb.archive_task(board, authority)
    assert kb.archive_task(board, relay)
    assert kb.get_task(board, relay).status == "archived"
    assert connections.held(board, relay)
    assert kb.get_task(board, consumer).status == "todo"
    assert kb.parent_states(board, consumer) == [(relay, "triage")]
    assert kb.claim_task(board, relay, claimer="default") is None
    assert board.execute("SELECT containment_episode FROM tasks WHERE id=?", (relay,)).fetchone()[0] == episode
    event = dict(board.execute("SELECT * FROM task_events WHERE task_id=? AND kind='containment_closed'", (relay,)).fetchone())
    payload = json.loads(event["payload"])
    assert payload["reason"] == "authority_archived"
    assert payload["authority"] == spec["authority"]
    assert payload["episode"] == episode
    assert not board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='containment_released'", (relay,)).fetchone()
    for sql in ("UPDATE tasks SET status='ready' WHERE id=?", "DELETE FROM tasks WHERE id=?"):
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            board.execute(sql, (relay,))
    for sql in ("UPDATE task_events SET payload='{}' WHERE uid=?", "DELETE FROM task_events WHERE uid=?"):
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            board.execute(sql, (event["uid"],))


@pytest.mark.parametrize("authority_status,producer,allowed", [
    ("running", False, False), ("done", False, True), ("done", True, False), ("archived", True, True),
])
def test_authority_terminal_policy(board, authority_status, producer, allowed):
    from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
    relay, consumer, authority, target, spec = lifecycle.setup(board)
    if producer:
        spec["producer"] = {**target, "task_id": kb.create_task(board, title="Producer", assignee="default")}
    episode = lifecycle.contain(board, relay, target, spec)["episode"]
    if authority_status == "done":
        assert kb.complete_task(board, authority, summary="Authority completed, retaining containment",
                                metadata={"containment_retained": [episode]})
    elif authority_status == "archived":
        assert kb.archive_task(board, authority)
    if allowed:
        assert kb.archive_task(board, relay)
        assert kb.get_task(board, relay).status == "archived"
    else:
        with pytest.raises(ContainmentRefused, match="authority"):
            kb.archive_task(board, relay)
        assert kb.get_task(board, relay).status == "triage"
        assert not board.execute("SELECT 1 FROM task_events WHERE kind='containment_closed'").fetchone()
    assert kb.get_task(board, consumer).status == "todo"


def test_terminal_writer_does_not_grant_ready_permission(board):
    from hermes_cli.kanban_containment_terminal import writer
    relay, _, _, target, spec = lifecycle.setup(board)
    lifecycle.contain(board, relay, target, spec)
    with kb.write_txn(board), writer(board, relay):
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            board.execute("UPDATE tasks SET status='ready' WHERE id=?", (relay,))


def test_archive_append_failure_rolls_back_all_receipts(board, monkeypatch):
    relay, _, authority, target, spec = lifecycle.setup(board)
    lifecycle.contain(board, relay, target, spec)
    assert kb.archive_task(board, authority)
    original = kb._append_event
    def fail(conn, task, kind, *args, **kwargs):
        if task == relay and kind == "archived":
            raise RuntimeError("fixture failure after closure")
        return original(conn, task, kind, *args, **kwargs)
    monkeypatch.setattr(kb, "_append_event", fail)
    with pytest.raises(RuntimeError, match="after closure"):
        kb.archive_task(board, relay)
    assert kb.get_task(board, relay).status == "triage"
    assert board.execute("SELECT COUNT(*) FROM containment_attestations").fetchone()[0] == 0
    assert not board.execute("SELECT 1 FROM task_events WHERE kind='containment_closed'").fetchone()
    assert connections.held(board, relay)


@pytest.mark.parametrize("case", ["unknown_marker", "missing_attestation", "changed_stop"])
def test_archive_refuses_unknown_or_changed_episode(board, case):
    from hermes_cli import kanban_containment_guard as guard
    from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
    from hermes_cli.kanban_db_sync import uuid7
    relay, _, authority, target, spec = lifecycle.setup(board)
    episode = lifecycle.contain(board, relay, target, spec)["episode"]
    assert kb.archive_task(board, authority)
    if case == "unknown_marker":
        with kb.write_txn(board), guard.authorized_write(board, relay):
            board.execute("UPDATE tasks SET containment_episode=? WHERE id=?", (uuid7(), relay))
    elif case == "missing_attestation":
        board.execute("DROP TRIGGER containment_local_stop_delete")
        board.execute("DELETE FROM containment_local_stops WHERE uid=?", (episode,))
    else:
        board.execute("DROP TRIGGER containment_event_update")
        stop = json.loads(board.execute("SELECT payload FROM task_events WHERE uid=?", (episode,)).fetchone()[0])
        stop["target"]["gateway"] = "changed-home"
        board.execute("UPDATE task_events SET payload=? WHERE uid=?", (json.dumps(stop), episode))
    with pytest.raises(ContainmentRefused):
        kb.archive_task(board, relay)
    assert kb.get_task(board, relay).status == "triage"


def test_local_archive_upgrades_preterminal_attestation_trigger(board):
    relay, _, authority, target, spec = lifecycle.setup(board)
    lifecycle.contain(board, relay, target, spec)
    board.execute("DROP TRIGGER containment_attestation_insert")
    board.execute("CREATE TRIGGER containment_attestation_insert BEFORE INSERT ON containment_attestations "
                  "WHEN NOT kanban_containment_write(NEW.task_id) "
                  "BEGIN SELECT RAISE(ABORT,'containment_pending'); END")
    assert kb.archive_task(board, authority)
    assert kb.archive_task(board, relay)


def test_all_active_episodes_close_atomically(board):
    from hermes_cli import kanban_containment_guard as guard
    from hermes_cli import kanban_containment_lifecycle as life
    from hermes_cli.kanban_containment_state import _payload, fingerprint
    relay, consumer, authority, target, spec = lifecycle.setup(board)
    first = lifecycle.contain(board, relay, target, spec)["episode"]
    second_authority = kb.create_task(board, title="Second frozen authority", assignee="default")
    payload = _payload(dict(board.execute("SELECT * FROM task_events WHERE uid=?", (first,)).fetchone()))
    payload["policy"]["authority"]["task_id"] = second_authority
    with kb.write_txn(board), guard.authorized_write(board, relay):
        second = life._append(board, relay, "contained", payload)
        board.execute("INSERT INTO containment_local_stops VALUES (?,?,?)", (relay, second, fingerprint(payload)))
    assert kb.archive_task(board, authority)
    with pytest.raises(life.ContainmentRefused, match="authority"):
        kb.archive_task(board, relay)
    assert not board.execute("SELECT 1 FROM task_events WHERE kind='containment_closed'").fetchone()
    assert kb.archive_task(board, second_authority)
    assert kb.archive_task(board, relay)
    closures = [json.loads(r[0])["episode"] for r in board.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='containment_closed'", (relay,))]
    assert set(closures) == {first, second} and len(closures) == 2
    assert kb.get_task(board, consumer).status == "todo"
