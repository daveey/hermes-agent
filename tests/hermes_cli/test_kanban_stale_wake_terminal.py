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


def test_a_push_strip_note_between_the_pages_does_not_lose_the_reopen(synced_conn):
    """Review of #1958: the restore read the newest note of any source, so a push strip written between the held
    row's page and the reopen's page left the card done over a legitimate Board reopen."""
    conn, tid = synced_conn, "t_01a1d1092f3caa32"
    _done_remote_card(conn, tid)
    sync.apply_remote_changes(conn, [_ch(3, "tasks", tid, {"status": "ready"}, version=2)], new_cursor=3)
    assert _status(conn, tid) == "done"
    with kbc.write_txn(conn):
        hold = ktg.terminal_hold(conn, tid)
        assert hold is not None
        ktg.note_push_strip(conn, tid, hold, "ready")
    sync.apply_remote_changes(conn, [_ev(4, tid, "unblocked", 1_794_520_600)], new_cursor=4)
    assert _status(conn, tid) == "ready"
    assert [n["source"] for n in _ignored(conn, tid)] == ["apply", "push"]


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


def test_divergence_repair_still_sends_an_uncaptured_local_reopen(live, kanban_home, svc):
    """Review of #1958: the run-less hold read only the replica, so a local reopen written while capture was off
    (the case the repair exists for) was adopted back to done. The live log's reopen wins."""
    tid = kb.create_task(live, title="pool card", assignee="worker")
    _round(kanban_home, svc)
    now = int(time.time())
    svc.hub(OTHER).ops(BOARD, [
        {"client_op_id": "w1-done", "op": "upsert", "table": "tasks", "uid": tid, "hlc": _hlc(1_000, OTHER),
         "fields": {"status": "done", "completed_at": now, "current_run_uid": None}},
        {"client_op_id": "w1-ev", "op": "upsert", "table": "task_events", "uid": kb_uuid(), "hlc": _hlc(1_000, OTHER),
         "fields": {"task_id": tid, "kind": "completed", "payload": "{}", "created_at": now}},
    ])
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying = 1")  # capture down: neither row nor event queued
        live.execute("UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?", (tid,))
        live.execute("INSERT INTO task_events (uid, task_id, kind, payload, created_at) VALUES (?, ?, 'reopened', "
                     "'{}', ?)", (kb_uuid(), tid, now + 5))
        live.execute("UPDATE kanban_sync_state SET applying = 0")
    shadow = kbc.connect(ksc.shadow_path(BOARD))
    try:
        assert tid in ksc.repair_divergence(live, shadow, ME)
    finally:
        shadow.close()


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


def test_an_ignored_probe_wake_drops_the_probe_so_the_next_sweep_runs_none(board, tmp_path):
    """Review of #1958 (nit 1): the skip returned before the probe row was deleted, so a stale scheduled copy kept
    probing every ``check_every`` (and held a probe slot) until the feed healed the row to done. The restore lint
    must not bring the dropped probe back either: it skips the held card before restoring."""
    runs = tmp_path / "runs"
    tid = _parked(board, wake_check=f"echo x >> {runs}")
    _stale_scheduled_copy_of_a_done_card(board, tid)
    now = int(time.time())
    kwc.run_due_wake_checks(board, now=now + 10 ** 6, wait=True)
    assert runs.read_text().count("x") == 2                            # one probe: an exit 0 is confirmed by a rerun
    assert board.execute("SELECT COUNT(*) FROM task_wake_checks WHERE task_id = ?", (tid,)).fetchone()[0] == 0
    assert kwc.run_due_wake_checks(board, now=now + 2 * 10 ** 6, wait=True)["submitted"] == []
    assert runs.read_text().count("x") == 2                            # no second probe
    assert board.execute("SELECT COUNT(*) FROM task_wake_checks WHERE task_id = ?", (tid,)).fetchone()[0] == 0
    assert kwc.RESTORED_EVENT not in _kinds(board, tid)
    assert _status(board, tid) == "scheduled"
    assert kwc.CONDITION_MET_EVENT not in _kinds(board, tid)


def test_an_ignored_wake_also_drops_the_cards_wake_episode(board):
    tid = _parked(board, wake_check="true")
    _stale_scheduled_copy_of_a_done_card(board, tid)
    with kb.write_txn(board):
        board.execute("INSERT OR REPLACE INTO task_wake_episodes (task_id, cmd, first_at, checks) "
                      "VALUES (?, 'true', 1, 3)", (tid,))
        assert kwc._wake(board, tid, kwc.CONDITION_MET_EVENT, {}) is None
    for table in ("task_wake_checks", "task_wake_episodes"):
        assert board.execute(f"SELECT COUNT(*) FROM {table} WHERE task_id = ?", (tid,)).fetchone()[0] == 0


@pytest.mark.parametrize("restored_before", [False, True])
def test_a_probe_dropped_for_a_held_card_comes_back_when_the_hold_lifts(board, restored_before):
    """Review of #2187 (nit 3): the held-card skip deletes the probe row and the restore lint skips a held card, so
    a reopen reaching the log while the local row still reads ``scheduled`` under the same park must not leave the
    card unwatched until its clock. With no ``RESTORED_EVENT`` after the park the lint re-registers the probe; with
    one already there (the probe was lost and restored once before) it wakes the card instead."""
    tid = _parked(board, wake_check="true")
    if restored_before:
        with kb.write_txn(board):
            board.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (tid,))
        assert kwc.restore_missing_checks(board)["restored"] == [tid]
    _stale_scheduled_copy_of_a_done_card(board, tid)
    kwc.run_due_wake_checks(board, now=int(time.time()) + 10 ** 6, wait=True)
    assert board.execute("SELECT COUNT(*) FROM task_wake_checks WHERE task_id = ?", (tid,)).fetchone()[0] == 0
    assert kwc.restore_missing_checks(board) == {"restored": [], "woken": []}  # still held: stays gone
    with kb.write_txn(board):
        kb._append_event(board, tid, "reopened", {"reason": "the Board reopened it"})
    assert ktg.terminal_hold(board, tid) is None and _status(board, tid) == "scheduled"
    out = kwc.restore_missing_checks(board)
    if restored_before:
        assert out == {"restored": [], "woken": [tid]}
        assert _status(board, tid) == "ready"
        assert kwc.LOST_EVENT in _kinds(board, tid)
    else:
        assert out == {"restored": [tid], "woken": []}
        assert board.execute("SELECT cmd FROM task_wake_checks WHERE task_id = ?", (tid,)).fetchone()[0] == "true"
        assert _status(board, tid) == "scheduled"


def test_a_held_card_whose_remote_probe_went_silent_writes_no_barrier_per_sweep(board, monkeypatch, tmp_path):
    """Review of #2187 (nit 2): the remote-silent restore branch reached ``_lost_wake -> _wake`` before any held
    check, and ``_wake``'s skip branch advances the episode barrier, so every sweep wrote one barrier upsert for a
    done card whose probe runs on another gateway until the feed healed the row."""
    tid = _parked(board, wake_check=f"test -f {tmp_path / 'flag'}")
    _stale_scheduled_copy_of_a_done_card(board, tid)
    monkeypatch.setattr(kwc, "own_gateway", lambda conn: ME)
    with kb.write_txn(board):
        board.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (tid,))
        marks = ",".join("?" for _ in kc.SCHEDULED_ENTRY_EVENTS)
        park_id, payload, parked_at = board.execute(
            f"SELECT id, payload, created_at FROM task_events WHERE task_id = ? AND kind IN ({marks}) "
            "ORDER BY id DESC LIMIT 1", (tid, *kc.SCHEDULED_ENTRY_EVENTS)).fetchone()
        board.execute("UPDATE task_events SET payload = ? WHERE id = ?",
                      (json.dumps({**json.loads(payload), "probe_host": OTHER}), park_id))
    silent = int(parked_at) + kwc.ALIVE_EVERY + 2 * 300 + kwc.SILENT_GRACE + 5

    def barrier_seq():
        row = board.execute("SELECT MAX(sequence) FROM task_wake_episode_barriers WHERE task_id = ?",
                            (tid,)).fetchone()
        return row[0] or 0

    assert kwc.restore_missing_checks(board, now=silent) == {"restored": [], "woken": []}
    first = barrier_seq()
    assert first == 0
    for i in range(1, 5):
        assert kwc.restore_missing_checks(board, now=silent + i * 60) == {"restored": [], "woken": []}
    assert barrier_seq() == first                                      # no barrier write beyond the first sweep
    assert _status(board, tid) == "scheduled"
    assert kwc.LOST_EVENT not in _kinds(board, tid)
    assert len(_ignored(board, tid)) == 1                              # one note, not one per sweep


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


def _probe_rows(conn, tid):
    return conn.execute("SELECT purpose, cmd FROM task_wake_checks WHERE task_id = ?", (tid,)).fetchall()


def test_a_wake_that_does_not_land_on_a_live_blocked_card_keeps_its_ask_probe(board):
    """PR #2187 post-merge mutant (c): only a held card's ``_wake`` drops the probe row. A live ``blocked`` card
    (not ``scheduled``, so the wake does not land) hit by a producer such as ``supervisor_request_wake`` keeps its
    ``ask_resolved`` row; hoisting the delete above the ``skip_wake`` guard silently dropped that live probe."""
    from hermes_cli import kanban_ask_resolved as kar
    tid = _parked(board)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status = 'blocked', human_request = ? WHERE id = ?",
                      (json.dumps({"action": "a", "resolved_check": "test -f /nonexistent-flag"}), tid))
        kar.sync_row(board, tid)
    assert [tuple(r) for r in _probe_rows(board, tid)] == [(kar.PURPOSE, "test -f /nonexistent-flag")]
    with kb.write_txn(board):
        assert kwc._wake(board, tid, "supervisor_request_wake", {}) is None
    assert _status(board, tid) == "blocked"
    assert [tuple(r) for r in _probe_rows(board, tid)] == [(kar.PURPOSE, "test -f /nonexistent-flag")]
    assert _ignored(board, tid) == []


def test_a_wake_that_lands_drops_the_cards_probe_row(board, tmp_path):
    """The other side: a live ``scheduled`` card whose wake lands no longer probes."""
    tid = _parked(board, wake_check=f"test -f {tmp_path / 'flag'}")
    assert len(_probe_rows(board, tid)) == 1
    with kb.write_txn(board):
        assert kwc._wake(board, tid, "supervisor_request_wake", {}) == "supervisor_request_wake"
    assert _status(board, tid) == "ready"
    assert _probe_rows(board, tid) == []


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


def _v7_at(ms: int) -> str:
    stamp = f"{ms:012x}"
    return f"{stamp[:8]}-{stamp[8:]}-7abc-8def-0123456789ab"


@pytest.mark.parametrize("first,second,held", [("completed", "reopened", None), ("reopened", "completed", 2)])
def test_terminal_hold_does_not_trust_a_backfilled_uid_clock(first, second, held):
    """``kanban_db_sync`` backfills a uid-less row with a v7 uid stamped at backfill time (here second 900 for a row
    of second 500). Its milliseconds are not event time, so two events of one second fall back to arrival order."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, uid TEXT, task_id TEXT, kind TEXT, payload TEXT, "
                 "created_at INTEGER)")
    conn.execute("INSERT INTO task_events VALUES (1, ?, 't', ?, '{}', 500)", (_v7_at(900_123), first))
    conn.execute("INSERT INTO task_events VALUES (2, NULL, 't', ?, '{}', 500)", (second,))
    hold = ktg.terminal_hold(conn, "t")
    assert (hold and hold["id"]) == held


def test_terminal_hold_still_orders_one_second_by_a_consistent_uid_clock():
    """A uid minted in the row's own second is event time: the later-minted reopen wins although it arrived first."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, uid TEXT, task_id TEXT, kind TEXT, payload TEXT, "
                 "created_at INTEGER)")
    conn.execute("INSERT INTO task_events VALUES (1, ?, 't', 'reopened', '{}', 500)", (_v7_at(500_700),))
    conn.execute("INSERT INTO task_events VALUES (2, ?, 't', 'completed', '{}', 500)", (_v7_at(500_200),))
    assert ktg.terminal_hold(conn, "t") is None


# --- unblock_task: ``unblocked`` is a reopen, and its automated callers act on the local row -----------------------
# PR #1958 review 5889419180 nit 1: a stale copy of a done card, auto-unblocked by the shared-blocker clear/probe,
# the quota release or a ``resolved_check``, went ``ready`` and wrote ``unblocked``, which both guards (here and
# hermes-hub#105) accept as an explicit reopen.

@pytest.mark.parametrize("stale", ["scheduled", "blocked"])
def test_unblock_of_a_stale_copy_of_a_done_card_does_not_reopen_it(board, stale):
    tid = _parked(board)
    _stale_scheduled_copy_of_a_done_card(board, tid)
    if stale == "blocked":
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    for _ in range(2):  # the shared-blocker probe and the quota release retry every tick
        assert kb.unblock_task(board, tid) is False
    assert _status(board, tid) == stale
    assert "unblocked" not in _kinds(board, tid)
    assert ktg.terminal_hold(board, tid) is not None                    # the push still strips the stale status
    [note] = _ignored(board, tid)                                     # one note, not one per tick
    assert (note["terminal_kind"], note["source"], note["wake_kind"]) == ("completed", "producer:unblocked",
                                                                           "unblocked")


def test_a_refused_unblock_runs_no_precondition_and_keeps_no_comment(board):
    """``kanban_ask_resolved`` passes both: its precondition writes ``ask_resolved`` and drops the wake check, so it
    must not run for a card the guard refuses; the note survives the comment's savepoint rollback."""
    tid = _parked(board)
    _stale_scheduled_copy_of_a_done_card(board, tid)
    ran = []
    assert kb.unblock_task(board, tid, comment=("dispatcher", "resolved_check exited 0"),
                           precondition=lambda c: ran.append(1)) is False
    assert ran == []
    assert [c for c in kb.list_comments(board, tid) if "resolved_check" in c.body] == []
    assert len(_ignored(board, tid)) == 1


def test_unblock_of_a_live_card_still_lands(board):
    """A card never completed, and one completed then explicitly reopened, unblock as before and leave no note."""
    fresh = _parked(board)
    again = _parked(board)
    with kb.write_txn(board):
        kb._append_event(board, again, "completed", {})
        kb._append_event(board, again, "reopened", {"reason": "more"})
    for tid in (fresh, again):
        assert kb.unblock_task(board, tid, comment=("david", "go")) is True
        assert _status(board, tid) == "ready"
        assert "unblocked" in _kinds(board, tid)
        assert _ignored(board, tid) == []


@pytest.mark.parametrize("parked", ["scheduled", "blocked"])
def test_a_review_reopen_after_the_completion_is_a_reopen(board, parked):
    """``review_reopened`` is in ``REOPEN_KINDS``: a card completed, then reopened through review and parked, is live.
    Without it ``unblock_task`` refuses the card and a wake skips it (PR #2186 post-merge mutant c1)."""
    tid = _parked(board)
    with kb.write_txn(board):
        kb._append_event(board, tid, "completed", {})
        kb._append_event(board, tid, "review_reopened", None)
        board.execute("UPDATE tasks SET status = ? WHERE id = ?", (parked, tid))
    assert ktg.terminal_hold(board, tid) is None
    assert kb.unblock_task(board, tid) is True
    assert _status(board, tid) == "ready"
    assert _ignored(board, tid) == []


def test_a_review_reopen_after_a_completion_and_reopen_cycle_lands_and_is_not_held(board):
    """The real path: done, reopened by an operator, back in ``review``; the review reopen still lands (PR #2186
    post-merge mutant c3 refused any card with a prior ``completed``), and the card is not held afterwards."""
    from hermes_cli.kanban_db_operator import reopen_done_task
    tid = kb.create_task(board, title="t", assignee="worker")
    claim = kb.claim_task(board, tid, claimer="worker")
    assert claim is not None
    assert kb.complete_task(board, tid, result="first pass", expected_run_id=claim.current_run_id) is True
    assert reopen_done_task(board, tid, reason="more", actor="david") == "ready"
    claim = kb.claim_task(board, tid, claimer="worker")
    assert claim is not None
    assert kb.request_review(board, tid, summary="second pass", expected_run_id=claim.current_run_id) is True
    assert _status(board, tid) == "review"
    assert kb.reopen_review_task(board, tid) is True
    assert _status(board, tid) == "ready"
    assert "review_reopened" in _kinds(board, tid)
    assert ktg.terminal_hold(board, tid) is None
    assert _ignored(board, tid) == []


def test_unblock_of_a_done_row_is_refused_without_a_note(board):
    """Not unblockable anyway: the row is already terminal, so there is no stale wake to record."""
    tid = kb.create_task(board, title="t", assignee="worker")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
        kb._append_event(board, tid, "completed", {})
    assert kb.unblock_task(board, tid) is False
    assert _status(board, tid) == "done"
    assert _ignored(board, tid) == []


# PR #2186 review 5891019707 nit 1: the human paths printed "cannot unblock <id> (not blocked/scheduled?)" for a row
# that does read blocked/scheduled, and ``reopen`` refused the stale row ("status is 'scheduled', not 'done'").

def _cli(*args):
    import argparse
    from hermes_cli import kanban
    from hermes_cli.kanban_parser import build_parser
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    return kanban.kanban_command(parser.parse_args(["kanban", *args]))


@pytest.fixture
def human(monkeypatch):
    from hermes_cli import kanban
    for key in (*kanban._AGENT_ENV_MARKERS, "HERMES_KANBAN_WORKER_TASK", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)


def test_cli_unblock_of_a_stale_copy_points_at_reopen_and_reopen_revives_it(board, human, capsys):
    tid = _parked(board)
    _stale_scheduled_copy_of_a_done_card(board, tid)
    assert _cli("unblock", tid) == 1
    err = capsys.readouterr().err
    assert f"cannot unblock {tid}: its completed event holds it done" in err
    assert f"hermes kanban reopen {tid} --reason" in err
    assert "not blocked/scheduled?" not in err
    assert _cli("reopen", tid, "--reason", "more work") == 0
    assert _status(board, tid) == "ready"
    assert ktg.terminal_hold(board, tid) is None
    reopened = [e for e in kb.list_events(board, tid) if e.kind == "reopened"][-1].payload
    assert (reopened["previous_status"], reopened["stale_row_status"]) == ("done", "scheduled")


def test_cli_unblock_of_a_card_that_is_not_held_keeps_the_generic_message(board, human, capsys):
    tid = kb.create_task(board, title="t", assignee="worker")                      # ready: nothing to unblock
    assert _cli("unblock", tid) == 1
    assert f"cannot unblock {tid} (not blocked/scheduled?)" in capsys.readouterr().err
    assert _cli("reopen", tid, "--reason", "x") == 1                              # reopen still refuses a live row


def test_unblock_refusal_of_an_archived_copy_names_no_reopen_and_the_dashboard_echoes_it(board):
    from plugins.kanban.dashboard import plugin_api
    tid = _parked(board)
    with kb.write_txn(board):
        kb._append_event(board, tid, "archived", {})
    text = ktg.unblock_refusal(board, tid)
    assert text.startswith(f"cannot unblock {tid}: its archived event holds it archived")
    assert "reopen `" not in text and "not revived" in text
    assert plugin_api._refusal_detail(board, tid, "ready") == text


# PR #2271 review 5893184151: reopen's stale-copy branch nulled ``current_run_id`` without closing the run it pointed
# at (``unblock_task`` reclaims it on the same statuses), and the CLI's archived-copy refusal still named unblock.

# PR #2381 post-merge mutants C4a/C4b: reopen accepts a stale copy in every ``_UNBLOCKABLE`` status, so each one's
# dangling run must be reclaimed, not only the ``scheduled`` one.
@pytest.mark.parametrize("stale", ["blocked", "scheduled", "triage"])
def test_reopen_of_a_stale_copy_with_a_live_run_pointer_ends_that_run(board, stale):
    from hermes_cli.kanban_db_operator import reopen_done_task
    tid = _parked(board)
    _stale_scheduled_copy_of_a_done_card(board, tid)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status = ? WHERE id = ?", (stale, tid))
        run_id = board.execute("INSERT INTO task_runs (task_id, profile, status, started_at) "
                               "VALUES (?, 'worker', 'running', ?)", (tid, int(time.time()))).lastrowid
        board.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, tid))
    assert ktg.stale_hold(board, tid)["status"] == "done"
    assert reopen_done_task(board, tid, reason="more work", actor="david") == "ready"
    run = board.execute("SELECT status, ended_at FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["ended_at"] is not None and run["status"] == "reclaimed"
    assert board.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0] is None


def test_reopen_refusal_text_is_none_for_a_done_held_copy(board):
    """PR #2381 post-merge mutant C5: reopen revives a done-held stale copy, so its refusal text is None while
    unblock's still points at reopen. ``_cmd_reopen`` never reaches this branch today (it accepts the done hold
    first), so only a direct assertion pins it."""
    tid = _parked(board)
    _stale_scheduled_copy_of_a_done_card(board, tid)
    assert ktg.unblock_refusal(board, tid, verb="reopen") is None
    assert ktg.unblock_refusal(board, tid).startswith(f"cannot unblock {tid}: its completed event holds it done")


def test_cli_reopen_of_an_archived_stale_copy_says_nothing_revives_it(board, human, capsys):
    tid = _parked(board)
    with kb.write_txn(board):
        kb._append_event(board, tid, "archived", {})
    assert _cli("reopen", tid, "--reason", "more work") == 1
    err = capsys.readouterr().err
    assert f"cannot reopen {tid}: its archived event holds it archived (this row is a stale copy)" in err
    assert "not revived by unblock or reopen" in err
    assert "use unblock/reopen-review" not in err
    assert _status(board, tid) == "scheduled"
