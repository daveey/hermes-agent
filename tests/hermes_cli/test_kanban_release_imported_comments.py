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


@pytest.mark.parametrize("entry", ["promote", "unblock", "tool"])
@pytest.mark.parametrize("broken", [None, "foreign", "no_claim", "wrong_run", "wrong_author", "missing",
                                    "unhashable_run"])
def test_delayed_legacy_local_writer(board, entry, broken, capsys):
    enable_sync(board)
    cid = comment(board)
    with kbc.connect_closing() as conn:
        with kbc.write_txn(conn):
            run = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, uid) "
                "VALUES (?, 'default', 'done', ?, 'writer-run')",
                (board, int(time.time()) - 300)).lastrowid
            payload = {"comment_id": cid, "author": "default", "writer_session": "other",
                       "writer_run_id": run, "writer_run_uid": "writer-run"}
            if broken == "wrong_run":
                payload["writer_run_id"] = run + 1
            if broken == "wrong_author":
                payload["author"] = "another"
            if broken == "unhashable_run":
                payload["writer_run_id"] = [run]
            # Both clocks drift 21 s: with co-minted uids the row is provable by write time alone
            # (test_legacy_lock_drift_resolves_by_uid_write_time), and this test is about run provenance.
            row_ms = int(conn.execute("SELECT created_at FROM task_comments WHERE id=?", (cid,)).fetchone()[0]) * 1000
            conn.execute("UPDATE task_comments SET uid=? WHERE id=?", (_uid_at(row_ms), cid))
            conn.execute("UPDATE task_events SET created_at=created_at+21, uid=?, payload=? "
                         "WHERE task_id=? AND kind='commented'", (_uid_at(row_ms + 21000), json.dumps(payload), board))
            if broken != "no_claim":
                kb._append_event(conn, board, "claimed", {
                    "holder": "peer" if broken == "foreign" else "owner", "run_uid": "writer-run"})
            if broken == "missing":
                conn.execute("DELETE FROM task_comments WHERE id=?", (cid,))
    before = snapshot(board)
    ok, output = release(board, entry, capsys)
    assert not ok, output
    assert snapshot(board) == before
    if broken is None:
        assert "Leave this card in triage" in output
        assert "Comment data unavailable" not in output
    ok, output = release(board, entry, capsys, cid)
    if broken:
        assert not ok and "Comment data unavailable" in output, output
        assert snapshot(board) == before
    else:
        assert ok, output
        assert snapshot(board)[0] == "ready"


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


def _uid_at(ms):
    from hermes_cli.kanban_db_sync import uuid7
    raw = uuid7().replace("-", "")
    h = f"{ms:012x}" + raw[12:]
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _lock_drift(tid, drift_s, uid_gap_ms):
    """t_01a1a1dea9c64d6a shape: no comment_uid, no transcript, row stamped `drift_s` before its event."""
    enable_sync(tid)
    cid = comment(tid, session="peer-session", age=8 * 86400, body="Ladder run note from a subagent")
    with kbc.connect_closing() as conn:
        row = conn.execute("SELECT * FROM task_comments WHERE id=?", (cid,)).fetchone()
        ms = (row["created_at"] + drift_s) * 1000 + 300  # live shape: both uids minted at the event time
        with kbc.write_txn(conn):
            conn.execute("UPDATE task_comments SET uid=? WHERE id=?", (_uid_at(ms), cid))
            ev = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='commented' "
                              "AND json_extract(payload, '$.comment_id')=?", (tid, cid)).fetchone()
            payload = json.loads(ev["payload"])
            payload.pop("comment_uid", None)
            payload.pop("writer_run_id", None)
            payload.pop("writer_run_uid", None)
            conn.execute("UPDATE task_events SET created_at=?, uid=?, payload=? WHERE id=?",
                         (row["created_at"] + drift_s, _uid_at(ms + uid_gap_ms), json.dumps(payload), ev["id"]))
    return cid


@pytest.mark.parametrize("entry", ["promote", "unblock", "tool"])
@pytest.mark.parametrize("drift_s,uid_gap_ms", [(5, 6), (7, 1)])
def test_legacy_lock_drift_resolves_by_uid_write_time(board, entry, drift_s, uid_gap_ms, capsys):
    """Replay of the two live events (5 s / 6 ms and 7 s / 1 ms): proven by uid time, acknowledged by --seen."""
    cid = _lock_drift(board, drift_s, uid_gap_ms)
    ok, output = release(board, entry, capsys, cid - 1)
    assert not ok and "Ladder run note" in output and "data unavailable" not in output, output
    ok, output = release(board, entry, capsys, cid)
    assert ok, output


@pytest.mark.parametrize("uid_gap_ms", [1500, 60000])
def test_legacy_uid_time_far_apart_stays_missing(board, uid_gap_ms, capsys):
    cid = _lock_drift(board, 5, uid_gap_ms)
    ok, output = release(board, "promote", capsys, cid + 1000)
    assert not ok and "data unavailable" in output, output


def test_comment_row_is_stamped_inside_the_write_lock(board, monkeypatch):
    """The producer half: a 5 s wait for the write lock no longer separates row and event stamps."""
    import contextlib
    clock = [1794426069.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    real = kb.write_txn

    @contextlib.contextmanager
    def slow_lock(conn, *args, **kwargs):
        clock[0] += 5  # waiting for another writer's lock
        with real(conn, *args, **kwargs):
            yield

    monkeypatch.setattr(kb, "write_txn", slow_lock)
    with kbc.connect_closing() as conn:
        cid = kb.add_comment(conn, board, "default", "stamped late", writer_kind="agent", writer_session="w")
        row_at = conn.execute("SELECT created_at FROM task_comments WHERE id=?", (cid,)).fetchone()[0]
        ev_at = conn.execute("SELECT created_at FROM task_events WHERE task_id=? AND kind='commented' "
                             "AND json_extract(payload, '$.comment_id')=?", (board, cid)).fetchone()[0]
    assert row_at == ev_at == 1794426074


def _uid_salted(ms, salt):
    h = f"{ms:012x}" + f"7{salt:03x}" + "8" + f"{salt:03x}" + f"{salt:012x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


@pytest.mark.parametrize("entry", ["promote", "unblock", "tool"])
@pytest.mark.parametrize("row_age", [8 * 86400, 2 * 86400 + 30])
def test_backfilled_uids_never_bind_a_spare_row(board, entry, row_age, capsys):
    """Adversarial review of #3629: sync-enable backfill mints one instant for old rows and events.

    An eventless row (``_insert_comment``: specify, decompose) by the same author must not stand in
    for a foreign comment that has not synced, however close the backfilled uids are.
    """
    enable_sync(board)
    now = int(time.time())
    enabled_ms = (now - 3600) * 1000
    spare = sync.RemoteChange(1, "task_comments", _uid_salted(enabled_ms + 2, 1), "upsert",
                              {"task_id": board, "author": "default",
                               "body": "Specified -- updated body and promoted to todo.",
                               "created_at": now - row_age}, 1, "", "peer")
    payload = {"author": "default", "len": 40, "comment_id": 77, "writer_kind": "agent",
               "writer_session": "peer-session"}
    event = sync.RemoteChange(2, "task_events", _uid_salted(enabled_ms + 5, 2), "upsert",
                              {"task_id": board, "kind": "commented", "created_at": now - 2 * 86400,
                               "payload": json.dumps(payload)}, 1, "", "peer")
    with kbc.connect_closing() as conn:
        assert sync.apply_remote_changes(conn, [spare, event], new_cursor=2, observers=False).applied == 2
        spare_id = conn.execute("SELECT id FROM task_comments WHERE uid=?", (spare.uid,)).fetchone()[0]
    before = snapshot(board)
    ok, output = release(board, entry, capsys, spare_id)
    assert not ok and "Comment data unavailable" in output, output
    assert snapshot(board) == before


@pytest.mark.parametrize("drift_s", [-2, 61])
def test_uid_rule_bounds_row_drift(board, drift_s, capsys):
    """Co-minted uids alone do not bind a row stamped after its event or more than a lock wait before it."""
    cid = _lock_drift(board, drift_s, 6)
    ok, output = release(board, "promote", capsys, cid + 1000)
    assert not ok and "data unavailable" in output, output
