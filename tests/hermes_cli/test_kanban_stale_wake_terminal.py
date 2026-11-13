"""A done card leaves done only through an explicit reopen: a stale wake never re-leases it. [daveey]

Board incidents 2026-11-11/12 (7 cards in 36 h, every one with a ``continuation_woken`` and no reopen):

* t_01a1d1092f3caa2f: the MacBook's copy of a pool card was ``scheduled``; its clock wake fired there at 13:51 PST
  (``status=ready``) while worker-1 completed the card at 16:14. The MacBook's push was stalled; when it drained at
  21:3x the row op carried a send-time HLC, beat the completion in the Board's per-field LWW, and worker-4 leased the
  done card again.
* t_01a1c7b27ca3b496 and five more: done for days, but worker-1's local rows still read ``scheduled``; its
  continuation sweep / wake-check restore woke them (11-11 14:22) and the Board leased them a minute later.

Tests (a)-(d) of the card: (a) a wake minted before a completion and applied/pushed after it leaves the card done,
(b) a producer (restore or continuation sweep) on a stale scheduled copy of a completed card does nothing, (d) a
legitimate wake still lands ready and an explicit reopen still reopens. (c), the lease, is hermes-hub's
``worker/test/stale-wake-terminal.test.ts``.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_terminal_guard as ktg
from hermes_cli import kanban_timed_park as ktp
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, OTHER, BOARD = "macbook", "worker-1", "default"


def _kinds(conn, tid):
    return [e.kind for e in kb.list_events(conn, tid)]


def _ignored(conn, tid):
    return [e.payload for e in kb.list_events(conn, tid) if e.kind == ktg.STALE_WAKE_IGNORED]


def _status(conn, tid):
    task = kb.get_task(conn, tid)
    return task.status if task else None


# --- (a) the feed apply ------------------------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def synced_conn(kanban_home):
    yield from support.synced_conn(kanban_home)


def _ch(seq, table, uid, fields, version=1):
    return sync.RemoteChange(seq, table, uid, "upsert", fields, version, f"{seq:013d}-0000-{OTHER}", OTHER, None)


def _ev(seq, tid, kind, created_at, payload=None, uid=None):
    return _ch(seq, "task_events", uid or f"01a1d109-{seq:04d}-7000-8000-000000000000",
               {"task_id": tid, "kind": kind, "payload": json.dumps(payload or {}), "created_at": created_at})


def _done_remote_card(conn, tid):
    sync.apply_remote_changes(conn, [
        _ch(1, "tasks", tid, {"title": "pool card", "status": "done", "created_at": 1_794_000_000,
                              "workspace_kind": "scratch", "assignee": "worker", "completed_at": 1_794_520_477}),
        _ev(2, tid, "completed", 1_794_520_477, {"result_len": 10}),
    ], new_cursor=2)
    assert _status(conn, tid) == "done"


def test_a_wake_minted_before_the_completion_and_applied_after_it_leaves_the_card_done(synced_conn):
    """t_01a1d1092f3caa2f: event 716387 (13:51:58, status=ready) arrived after 694876 ``completed`` (16:14:37)."""
    conn, tid = synced_conn, "t_01a1d1092f3caa2f"
    _done_remote_card(conn, tid)
    sync.apply_remote_changes(conn, [
        _ch(3, "tasks", tid, {"status": "ready", "current_run_uid": None}, version=2),
        _ev(4, tid, "continuation_woken", 1_794_520_318, {"status": "ready", "wake_at": 1_794_520_160}),
    ], new_cursor=4)
    task = kb.get_task(conn, tid)
    assert task.status == "done"
    assert "continuation_woken" in _kinds(conn, tid)                # the wake stays in history
    [note] = _ignored(conn, tid)
    assert (note["terminal_kind"], note["held_status"], note["source"]) == ("completed", "ready", "apply")
    assert conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE row_uid = ? AND table_name = 'tasks'",
                        (tid,)).fetchone()[0] == 0                   # an apply, not a local edit to push


def test_an_explicit_reopen_after_the_completion_still_reopens(synced_conn):
    conn, tid = synced_conn, "t_01a1d1092f3caa30"
    _done_remote_card(conn, tid)
    sync.apply_remote_changes(conn, [
        _ch(3, "tasks", tid, {"status": "ready"}, version=2),
        _ev(4, tid, "reopened", 1_794_520_600, {"reason": "follow-up"}),
    ], new_cursor=4)
    assert _status(conn, tid) == "ready"
    assert _ignored(conn, tid) == []


def test_a_reopen_whose_event_is_in_the_next_page_still_reopens(synced_conn):
    """A page boundary between the row and its reopen event: held done first, then given back."""
    conn, tid = synced_conn, "t_01a1d1092f3caa31"
    _done_remote_card(conn, tid)
    sync.apply_remote_changes(conn, [_ch(3, "tasks", tid, {"status": "ready"}, version=2)], new_cursor=3)
    assert _status(conn, tid) == "done"
    sync.apply_remote_changes(conn, [_ev(4, tid, "unblocked", 1_794_520_600)], new_cursor=4)
    assert _status(conn, tid) == "ready"


# --- (a) the push: a wake fired on a stale copy before the completion reached it -----------------------------------

@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    sync.set_mode(conn, "primary")
    try:
        yield conn
    finally:
        conn.close()


def _round(kanban_home, svc):
    return ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)


def _hlc(offset_ms: int, gw: str) -> str:
    return f"{int(time.time() * 1000) + offset_ms:013d}-0000-{gw}"


def test_a_wake_pushed_after_a_remote_completion_does_not_reopen_the_board_card(live, kanban_home, svc):
    """The MacBook fires its clock wake while its pull is behind; the push drains after the completion."""
    tid = kb.create_task(live, title="pool card", assignee="worker")
    kb.claim_task(live, tid)
    kb.schedule_task(live, tid, reason="SCHEDULED_UNTIL=2026-11-12T21:49:20Z ci", clock_only=True)
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    assert svc.board(BOARD)["rows"]["tasks"][tid]["status"] == "scheduled"

    assert kc.wake_due_continuations(live, now=int(time.time()) + 10 ** 7) == [tid]  # the stale local wake
    svc.hub(OTHER).ops(BOARD, [  # meanwhile worker-1 completed the card on the Board (HLC: after the park)
        {"client_op_id": "w1-done", "op": "upsert", "table": "tasks", "uid": tid, "hlc": _hlc(-1, OTHER),
         "fields": {"status": "done", "completed_at": int(time.time()), "current_run_uid": None}},
        {"client_op_id": "w1-ev", "op": "upsert", "table": "task_events", "uid": kb_uuid(), "hlc": _hlc(0, OTHER),
         "fields": {"task_id": tid, "kind": "completed", "payload": "{}", "created_at": int(time.time())}},
    ])
    assert svc.board(BOARD)["rows"]["tasks"][tid]["status"] == "done"
    # The drained push's send-time HLC (minted later, in the next round) is newer than the completion's.
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    assert svc.board(BOARD)["rows"]["tasks"][tid]["status"] == "done"
    assert live.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "done"
    assert any(n["source"] in ("push", "apply") for n in _ignored(live, tid))


def kb_uuid() -> str:
    return sync.uuid7()


def test_divergence_repair_does_not_send_a_stale_wake_over_the_replicas_done_card(live, kanban_home, svc):
    """The live row missed the completion (a woken ``ready``, no run) while the replica has it: the live copy is the
    stale one, not an uncaptured local edit, so the repair sends nothing."""
    tid = kb.create_task(live, title="pool card", assignee="worker")
    _round(kanban_home, svc)
    svc.hub(OTHER).ops(BOARD, [
        {"client_op_id": "w1-done", "op": "upsert", "table": "tasks", "uid": tid, "hlc": _hlc(1_000, OTHER),
         "fields": {"status": "done", "completed_at": int(time.time()), "current_run_uid": None}},
        {"client_op_id": "w1-ev", "op": "upsert", "table": "task_events", "uid": kb_uuid(), "hlc": _hlc(1_000, OTHER),
         "fields": {"task_id": tid, "kind": "completed", "payload": "{}", "created_at": int(time.time())}},
    ])
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying = 1")  # no capture: the row only missed the feed
        live.execute("UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?", (tid,))
        live.execute("UPDATE kanban_sync_state SET applying = 0")
    shadow = kbc.connect(ksc.shadow_path(BOARD))
    try:
        assert tid not in ksc.repair_divergence(live, shadow, ME)
    finally:
        shadow.close()
    _round(kanban_home, svc)
    assert svc.board(BOARD)["rows"]["tasks"][tid]["status"] == "done"


# --- (b) the producers ---------------------------------------------------------------------------------------------

@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    getattr(kwc, "_RESTORE_SEEN", {}).clear()
    with kbc.connect_closing() as conn:
        yield conn
    kwc._shutdown_for_tests()


def _parked(conn, *, wake_check=None):
    tid = kb.create_task(conn, title="t", assignee="worker")
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    with kb.write_txn(conn):
        assert ktp.park(conn, tid, reason="SCHEDULED_UNTIL=2026-11-10T17:07:04Z wait", requested_kind="time",
                        source_status="running", wake_check=wake_check, check_every=300,
                        awaiting={"kind": "external", "ref": "build 559", "why": "flag file"} if wake_check
                        else {"kind": "clock", "why": "a soak window"},
                        clock_only=not wake_check)
    return tid


def _stale_scheduled_copy_of_a_done_card(conn, tid):
    """The completion's event reached this file, the row did not (worker-1's six cards)."""
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "completed", {"result_len": 12})
    assert _status(conn, tid) == "scheduled"


def test_a_restored_wake_for_an_already_completed_card_does_nothing(board, tmp_path):
    tid = _parked(board, wake_check=f"test -f {tmp_path / 'flag'}")
    _stale_scheduled_copy_of_a_done_card(board, tid)
    with kb.write_txn(board):
        board.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (tid,))
    for _ in range(2):  # a second loss would otherwise wake it
        assert kwc.restore_missing_checks(board) == {"restored": [], "woken": []}
    assert _status(board, tid) == "scheduled"
    assert kwc.RESTORED_EVENT not in _kinds(board, tid)
    [note] = _ignored(board, tid)                                     # one note, not one per sweep
    assert (note["terminal_kind"], note["source"]) == ("completed", f"producer:{kwc.RESTORED_EVENT}")


def test_a_due_continuation_on_a_stale_copy_of_a_completed_card_does_nothing(board):
    tid = _parked(board)
    _stale_scheduled_copy_of_a_done_card(board, tid)
    for _ in range(2):
        assert kc.wake_due_continuations(board, now=int(time.time()) + 10 ** 7) == []
    assert _status(board, tid) == "scheduled"
    assert kc.CONTINUATION_WOKEN_EVENT not in _kinds(board, tid)
    assert len(_ignored(board, tid)) == 1


def test_a_passing_probe_on_a_stale_copy_of_a_completed_card_does_not_wake_it(board):
    tid = _parked(board, wake_check="true")
    _stale_scheduled_copy_of_a_done_card(board, tid)
    kwc.run_due_wake_checks(board, now=int(time.time()) + 10 ** 6, wait=True)
    assert _status(board, tid) == "scheduled"
    assert kwc.CONDITION_MET_EVENT not in _kinds(board, tid)
    assert [n["wake_kind"] for n in _ignored(board, tid)] == [kwc.CONDITION_MET_EVENT]


def test_an_implied_wake_on_a_stale_copy_of_a_completed_card_does_nothing(board):
    """A ``scheduled`` row with no park event (``_enforce_wake_invariant``'s implied wake)."""
    tid = kb.create_task(board, title="t", assignee="worker")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status = 'scheduled' WHERE id = ?", (tid,))
        kb._append_event(board, tid, "completed", {})
    assert kc.wake_due_continuations(board, now=int(time.time()) + 10 ** 7) == []
    assert _status(board, tid) == "scheduled"
    assert len(_ignored(board, tid)) == 1


def test_a_legitimate_wake_of_a_scheduled_card_still_lands_ready(board):
    """(d): a card completed once, reopened, then parked wakes as before."""
    tid = _parked(board)
    with kb.write_txn(board):
        kb._append_event(board, tid, "completed", {})
        kb._append_event(board, tid, "reopened", {"reason": "more"})
    assert kc.wake_due_continuations(board, now=int(time.time()) + 10 ** 7) == [tid]
    assert _status(board, tid) == "ready"
    assert _ignored(board, tid) == []


def test_terminal_hold_orders_by_origin_not_by_arrival():
    """The reopen arrived first (lower id) but was minted after the completion: it still counts."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, uid TEXT, task_id TEXT, kind TEXT, payload TEXT, "
                 "created_at INTEGER)")
    conn.execute("INSERT INTO task_events VALUES (1, NULL, 't', 'reopened', '{}', 200)")
    conn.execute("INSERT INTO task_events VALUES (2, NULL, 't', 'completed', '{}', 100)")
    assert ktg.terminal_hold(conn, "t") is None
    conn.execute("INSERT INTO task_events VALUES (3, NULL, 't', 'continuation_woken', '{\"status\":\"ready\"}', 300)")
    assert ktg.terminal_hold(conn, "t") is None
    conn.execute("INSERT INTO task_events VALUES (4, NULL, 't', 'completed', '{}', 400)")
    assert ktg.terminal_hold(conn, "t")["id"] == 4
