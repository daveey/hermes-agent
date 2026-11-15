"""Release watermarks use local IDs even when comment events came from a peer. [daveey]"""
import json
import time

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from tests.hermes_cli.test_kanban_release_unread_comments import board as _board, comment, release, snapshot

board = _board  # Re-export the shared fixture without shadowing an imported name.


def enable_sync(tid):
    from hermes_constants import get_hermes_home
    from tests.hermes_cli.kanban_sync_support import write_sync_config
    write_sync_config(get_hermes_home(), True)
    kb._INITIALIZED_PATHS.clear()
    with kbc.connect_closing() as conn:
        sync.apply_sync_schema_steps(conn)
        sync.set_mode(conn, "shadow")
        sync.set_identity(conn, "test-board", "owner")
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET created_on=? WHERE id=?", ("owner", tid))


def imported(tid, source_id, *, portable=True, age=180, session="peer-session", suffix="one"):
    now = int(time.time()) - age
    uid = "remote-comment-" + suffix
    payload = {"comment_id": source_id, "writer_session": session, "writer_kind": "agent"}
    if portable:
        payload["comment_uid"] = uid
    changes = [
        sync.RemoteChange(1, "task_comments", uid, "upsert",
                          {"task_id": tid, "author": "default", "body": "Retracted: do not release " + suffix,
                           "created_at": now}, 1, "", "peer"),
        sync.RemoteChange(2, "task_events", "remote-event-" + suffix, "upsert",
                          {"task_id": tid, "kind": "commented", "created_at": now,
                           "payload": json.dumps(payload)}, 1, "", "peer"),
    ]
    with kbc.connect_closing() as conn:
        assert sync.apply_remote_changes(conn, changes, new_cursor=2, observers=False).applied == 2
        cid = conn.execute("SELECT id FROM task_comments WHERE uid=?", (uid,)).fetchone()[0]
        from hermes_cli.kanban_foreign_card import refusal
        assert refusal(conn, tid) is None
    return cid


@pytest.mark.parametrize("entry", ["promote", "unblock", "tool"])
@pytest.mark.parametrize("portable", [True, False])
@pytest.mark.parametrize("collision", [True, False])
def test_imported_owner_release_acknowledges_local_id(board, entry, portable, collision, capsys):
    enable_sync(board)
    older = comment(board, session="reader", age=300, body="Already read local note")
    cid = imported(board, older if collision else 900001, portable=portable)
    assert cid != older
    before = snapshot(board)
    for seen in (None, older):
        ok, output = release(board, entry, capsys, seen)
        assert not ok, output
        assert "unread_comments" in output and "Retracted: do not release" in output
        assert f"--seen {cid}" in output
        assert "Already read local note" not in output
        assert snapshot(board) == before
    ok, output = release(board, entry, capsys, cid)
    assert ok, output


@pytest.mark.parametrize("portable", [True, False])
@pytest.mark.parametrize("session,age", [("reader", 180), (None, 180), ("peer-session", 3660)])
def test_imported_policy_exceptions(board, portable, session, age, capsys):
    enable_sync(board)
    imported(board, 900001, portable=portable, session=session, age=age)
    ok, output = release(board, "promote", capsys)
    assert ok, output


@pytest.mark.parametrize("portable", [True, False])
def test_imported_explicit_watermark_has_no_age_limit(board, portable, capsys):
    enable_sync(board)
    older = comment(board, session="reader", age=8000)
    cid = imported(board, 900001, portable=portable, age=7200)
    ok, output = release(board, "promote", capsys, older)
    assert not ok and f"--seen {cid}" in output
    ok, output = release(board, "promote", capsys, cid)
    assert ok, output


@pytest.mark.parametrize("portable", [True, False])
def test_missing_imported_comment_is_not_acknowledged_by_colliding_id(board, portable, capsys):
    enable_sync(board)
    older = comment(board, session="reader", age=300)
    cid = imported(board, older, portable=portable)
    with kbc.connect_closing() as conn:
        with kbc.write_txn(conn):
            conn.execute("DELETE FROM task_comments WHERE id=?", (cid,))
    before = snapshot(board)
    for seen in (None, older, cid):
        ok, output = release(board, "promote", capsys, seen)
        assert not ok and "Comment data unavailable" in output
        assert snapshot(board) == before
    restored = imported(board, older, portable=portable)
    ok, output = release(board, "promote", capsys)
    assert not ok and "Retracted: do not release" in output
    ok, output = release(board, "promote", capsys, restored)
    assert ok, output


@pytest.mark.parametrize("entry", ["promote", "unblock", "tool"])
@pytest.mark.parametrize("portable", [False, True])
@pytest.mark.parametrize("sibling_session", ["peer-session", "reader", None])
@pytest.mark.parametrize("sibling_portable", [True, False])
def test_partial_sync_cannot_reuse_acknowledged_sibling(
        board, entry, portable, sibling_session, sibling_portable, capsys, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1794700000)
    enable_sync(board)
    read_id = imported(board, 800001, portable=sibling_portable, suffix="read", session=sibling_session)
    payload = {"comment_id": 800002, "author": "default", "writer_session": "peer-session"}
    if portable:
        payload["comment_uid"] = "remote-comment-unread"
    change = sync.RemoteChange(3, "task_events", "remote-event-unread", "upsert",
                              {"task_id": board, "kind": "commented",
                               "created_at": int(time.time()) - 180,
                               "payload": json.dumps(payload)}, 1, "", "peer")
    with kbc.connect_closing() as conn:
        assert sync.apply_remote_changes(conn, [change], new_cursor=3, observers=False).applied == 1
    before = snapshot(board)
    ok, output = release(board, entry, capsys, read_id)
    assert not ok and "Comment data unavailable" in output, output
    assert snapshot(board) == before
    restored = imported(board, 800002, portable=portable, suffix="unread")
    ok, output = release(board, entry, capsys, read_id)
    assert not ok and "do not release unread" in output, output
    ok, output = release(board, entry, capsys, restored)
    assert ok, output


@pytest.mark.parametrize("candidates,missing", [
    ({1: [1], 2: [1]}, {1, 2}),
    ({1: [1, 2], 2: [1]}, set()),  # Reassign the first event, rather than greedily fail.
    ({1: [1], 2: [1], 3: [1, 2, 3]}, {1, 2, 3}),  # Equal totals do not prove Hall coverage.
    ({1: [], 2: [2]}, {1}),  # Missing unrelated history does not poison a complete component.
    ({1: [1, 2], 2: [2, 3], 3: [1]}, set()),
])
def test_event_coverage_requires_injective_matching(candidates, missing):
    from hermes_cli.kanban_release_comments import _missing_coverage
    resolved = {eid: [{"id": rid} for rid in rows] for eid, rows in candidates.items()}
    assert _missing_coverage(resolved) == missing


def test_writer_stamps_actual_comment_uid(board):
    enable_sync(board)
    cid = comment(board)
    with kbc.connect_closing() as conn:
        uid = conn.execute("SELECT uid FROM task_comments WHERE id=?", (cid,)).fetchone()[0]
        payload = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='commented'",
                                          (board,)).fetchone()[0])
    assert uid and payload["comment_uid"] == uid


def test_legacy_ambiguous_same_second_lists_all_candidates(board, capsys, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1794700000)
    enable_sync(board)
    first = imported(board, 800001, portable=False, suffix="first")
    second = imported(board, 800002, portable=False, suffix="second")
    ok, output = release(board, "promote", capsys, first)
    assert not ok and "do not release second" in output
    ok, output = release(board, "promote", capsys, second)
    assert ok, output
