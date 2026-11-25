"""A pull backlog never starves the outbox (t_01a1d36ddcea59fe, David-MBP 2026-11-12: 1747 local edits waited 9 h).

A round pushes before and after its pulls, each pull reads at most ``PULL_PAGE_BUDGET`` pages per database, a round
that stops behind the feed head is rerun at once, one process pulls a board's files at a time, and the age of the
oldest pending op is shown and alarmed on. The Board is the in-memory fake (``kanban_sync_fake_board``) served one
change per feed page, so a 60-change backlog is 60 pages.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_backlog as backlog
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_outbox_age as outbox_age
from hermes_cli import kanban_worker_placement as wp
from hermes_cli.kanban_sync_guard import board_sync_lock
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli import test_kanban_worker_reserve_client as reserve_fixtures
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, OTHER, BOARD = "macbook", "mini", "default"
BACKLOG_PAGES = 60

conn = reserve_fixtures.conn  # a primary board as gateway "hub" (dispatch_tick tests)


@pytest.fixture(autouse=True)
def _fresh_loop_state(monkeypatch):
    """Backlog flags and outbox-age verdicts are process-wide (the sync loop's): no test inherits another's."""
    backlog.reset()
    outbox_age.reset()
    from cron import restart_rerun
    monkeypatch.setattr(restart_rerun, "host_fully_awake", lambda: True)
    yield
    backlog.reset()
    outbox_age.reset()


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    c = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(c, BOARD, ME)
    sync.set_mode(c, "primary")
    try:
        yield c
    finally:
        c.close()


class PagedHub:
    """The fake Board's hub for one gateway, serving ONE change per feed page and logging the call order."""

    def __init__(self, svc, gateway, log):
        self._inner, self.log = svc.hub(gateway), log
        self.ident = self._inner.ident

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def changes(self, board, since, limit=500):
        self.log.append(("changes", since))
        return self._inner.changes(board, since, limit=1)

    def ops(self, board, ops):
        self.log.append(("ops", [op["uid"] for op in ops]))
        return self._inner.ops(board, ops)


def _other_creates(svc, n):
    hub = svc.hub(OTHER)
    for i in range(n):
        hub.ops(BOARD, [{"client_op_id": f"other-{i}", "op": "upsert", "table": "tasks", "uid": f"t_other{i:03d}",
                         "hlc": f"{1:013d}-0000-{OTHER}", "fields": {"title": f"o{i}", "status": "ready",
                                                                     "created_at": 1}}])


def _round(kanban_home, hub):
    return ksc.sync_board(BOARD, kanban_home / "kanban.db", hub, ME)


def _replica_state():
    with kbc.connect_closing(ksc.shadow_path(BOARD)) as shadow:
        st = sync.sync_state(shadow) or {}
        return st.get("cursor"), shadow.execute("SELECT pulled_at FROM kanban_sync_state").fetchone()[0]


# --- push before pull (RED on main: the op went out only after all 120 pages) ----------------------------------

def test_a_pending_op_is_sent_before_the_second_backlog_page(live, svc, kanban_home):
    _other_creates(svc, BACKLOG_PAGES)
    tid = kb.create_task(live, title="local edit")
    log: list = []
    result = _round(kanban_home, PagedHub(svc, ME, log))
    assert result.error is None
    pages = [i for i, (kind, _) in enumerate(log) if kind == "changes"]
    sent = [i for i, (kind, uids) in enumerate(log) if kind == "ops" and tid in uids]
    assert sent and len(pages) >= 2
    assert sent[0] < pages[1], log[:5]
    assert svc.board(BOARD)["rows"]["tasks"][tid]["title"] == "local edit"


def test_the_backlog_drains_over_rounds_and_pulled_at_marks_only_the_head(live, svc, kanban_home):
    _other_creates(svc, BACKLOG_PAGES)
    log: list = []
    hub = PagedHub(svc, ME, log)
    first = _round(kanban_home, hub)
    # 20 pages for the replica, then the round ended anyway; the never-pulled live file read one page and rebuilt
    # from a snapshot instead of replaying the feed (test_kanban_sync_cold_live).
    assert first.behind and first.pulled == backlog.PULL_PAGE_BUDGET
    assert sum(1 for kind, _ in log if kind == "changes") == backlog.PULL_PAGE_BUDGET + 1
    cursor, pulled_at = _replica_state()
    assert cursor == backlog.PULL_PAGE_BUDGET and pulled_at is None  # not at the head: never stamped
    path = kanban_home / "kanban.db"
    assert backlog.due([(BOARD, path)]) == [(BOARD, path)]
    rounds = 1
    while _round(kanban_home, hub).behind:
        rounds += 1
        assert rounds < 10
    cursor, pulled_at = _replica_state()
    assert cursor == svc.board(BOARD)["seq"] and pulled_at is not None
    assert backlog.due([(BOARD, path)]) == []
    assert sync.sync_state(live)["cursor"] == svc.board(BOARD)["seq"]


# --- the fence: a pushed status survives the older copies still in the backlog --------------------------------

def test_a_pushed_status_survives_older_versions_of_its_row_in_the_backlog(live, svc, kanban_home):
    tid = kb.create_task(live, title="mine", assignee="worker")
    assert _round(kanban_home, svc.hub(ME)).error is None
    # Another gateway edits the card (an older Board version than our coming push), then a backlog piles up.
    svc.hub(OTHER).ops(BOARD, [{"client_op_id": "other-prio", "op": "upsert", "table": "tasks", "uid": tid,
                                "hlc": f"{time.time_ns() // 1_000_000 - 60_000:013d}-0000-{OTHER}",
                                "fields": {"priority": 7, "status": "todo"}}])
    _other_creates(svc, BACKLOG_PAGES)
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    hub = PagedHub(svc, ME, [])
    _round(kanban_home, hub)
    acked = live.execute("SELECT sync_version FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    assert acked == svc.board(BOARD)["rows"]["tasks"][tid]["version"]
    assert kb.get_task(live, tid).status == "blocked"
    while _round(kanban_home, hub).behind:
        pass
    task = kb.get_task(live, tid)
    assert task.status == "blocked"  # the older feed copies (status todo) were fenced off
    assert svc.board(BOARD)["rows"]["tasks"][tid]["status"] == "blocked"


def test_a_round_left_behind_holds_a_pending_run_start_over_the_boards_end(kanban_home):
    """Review 5887313221 finding 1: the host slept, the Board reclaimed its lease (the run's end sits in a page the
    round does not reach) and the run's start is still pending. The send after a budget-ended pull must hold it:
    sent whole, it overwrote ``reclaimed`` with ``running`` on the Board, and its ack fenced the end off locally."""
    from tests.hermes_cli import test_kanban_sync_run_rows as run_rows
    svc = FakeService(lease_runs=True)
    support.write_sync_config(kanban_home, True)
    c = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(c, BOARD, ME)
    sync.set_mode(c, "primary")
    previous = sync.set_lease_client(ksc.HubLeaseClient(svc.hub(ME)))
    try:
        tid, uid = run_rows._claimed(c, kanban_home, svc, "asleep")
        assert c.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name = 'task_runs' AND row_uid = ? "
                         "AND state = 'pending'", (uid,)).fetchone()  # the start has not reached the Board
        c.execute("UPDATE tasks SET claim_expires = ?, worker_pid = NULL WHERE id = ?", (int(time.time()) - 5, tid))
        assert kb.release_stale_claims(c) == 0
        _other_creates(svc, BACKLOG_PAGES)
        run_rows._expire_board_lease(svc, tid, uid)
        hub = PagedHub(svc, ME, [])
        assert _round(kanban_home, hub).behind
        assert run_rows._board_run(svc, uid)["status"] == "reclaimed"  # not reopened by the held start
        while _round(kanban_home, hub).behind:
            assert run_rows._board_run(svc, uid)["status"] == "reclaimed"
        _round(kanban_home, hub)
        run_rows._assert_ended(c, svc, uid, "reclaimed")
    finally:
        sync.set_lease_client(previous)
        c.close()


def test_a_wake_on_a_card_the_board_completed_waits_for_the_head_while_a_live_one_goes_first(live, svc, kanban_home):
    """CI on 71d46e0099: the pre-pull send pushed a stale wake (``status=ready``) for a card the Board had completed
    in a page not pulled yet, so #1958's push strip never saw the completion and the Board reopened the card. A
    status whose Board row is done waits for the pull to reach the head; a wake on a live card still goes first."""
    stale = kb.create_task(live, title="done elsewhere", assignee="worker")
    woke = kb.create_task(live, title="still parked", assignee="worker")
    with kbc.write_txn(live):  # both parked, and the Board knows it
        live.execute("UPDATE tasks SET status = 'scheduled' WHERE id IN (?, ?)", (stale, woke))
    assert _round(kanban_home, svc.hub(ME)).error is None
    assert svc.board(BOARD)["rows"]["tasks"][woke]["status"] == "scheduled"
    _other_creates(svc, BACKLOG_PAGES)  # the completion lands after the backlog: in a page round 1 does not reach
    now = time.time_ns() // 1_000_000
    svc.hub(OTHER).ops(BOARD, [
        {"client_op_id": "w1-done", "op": "upsert", "table": "tasks", "uid": stale,
         "hlc": f"{now:013d}-0000-{OTHER}", "fields": {"status": "done", "completed_at": int(time.time())}},
        {"client_op_id": "w1-ev", "op": "upsert", "table": "task_events", "uid": sync.uuid7(),
         "hlc": f"{now + 1:013d}-0000-{OTHER}",
         "fields": {"task_id": stale, "kind": "completed", "payload": "{}", "created_at": int(time.time())}}])
    assert svc.board(BOARD)["rows"]["tasks"][stale]["status"] == "done"
    with kbc.write_txn(live):  # both local wakes, minted before this host pulled the completion
        live.execute("UPDATE tasks SET status = 'ready' WHERE id IN (?, ?)", (stale, woke))
    log: list = []
    hub = PagedHub(svc, ME, log)
    assert _round(kanban_home, hub).behind
    pages = [i for i, (kind, _) in enumerate(log) if kind == "changes"]
    sent = [i for i, (kind, uids) in enumerate(log) if kind == "ops" and woke in uids]
    assert sent and sent[0] < pages[1]  # the live card's wake did not wait for the backlog
    assert not any(kind == "ops" and stale in uids for kind, uids in log)  # the Board's done row held it
    rows = svc.board(BOARD)["rows"]["tasks"]
    assert rows[stale]["status"] == "done" and rows[woke]["status"] == "ready"
    while _round(kanban_home, hub).behind:
        assert svc.board(BOARD)["rows"]["tasks"][stale]["status"] == "done"
    _round(kanban_home, hub)
    assert svc.board(BOARD)["rows"]["tasks"][stale]["status"] == "done"
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE row_uid = ? AND state = 'pending'",
                            (stale,)).fetchone()  # stripped and sent at the head, not left pending


# --- a budget-ended round still finishes ---------------------------------------------------------------------

def test_a_round_ending_its_page_budget_still_pushes_resolves_refreshes_and_reevaluates(live, svc, kanban_home,
                                                                                      monkeypatch):
    _other_creates(svc, BACKLOG_PAGES)
    calls: list = []
    real_push = ksc.push

    def push(*a, **kw):
        calls.append("push")
        return real_push(*a, **kw)
    monkeypatch.setattr(ksc, "push", push)
    from hermes_cli import kanban_sync_rejections
    real_resolve = kanban_sync_rejections.resolve_rejections
    monkeypatch.setattr(kanban_sync_rejections, "resolve_rejections",
                        lambda *a, **kw: calls.append("resolve") or real_resolve(*a, **kw))
    real_refresh = sync.refresh_remote_parents
    monkeypatch.setattr(sync, "refresh_remote_parents", lambda *a, **kw: calls.append("refresh") or real_refresh(*a, **kw))
    monkeypatch.setattr(wp, "reevaluate", lambda *a, **kw: calls.append("reevaluate") or 0)
    log: list = []
    hub = PagedHub(svc, ME, log)
    real_changes = hub.changes
    hub.changes = lambda *a, **kw: calls.append("page") or real_changes(*a, **kw)
    result = _round(kanban_home, hub)
    assert result.behind and result.error is None
    pages = [i for i, c in enumerate(calls) if c == "page"]
    after = calls[pages[-1] + 1:]
    assert calls[:2] == ["push", "resolve"]  # before the pulls
    assert ["refresh", "push", "resolve", "reevaluate"] == [c for c in after if c != "page"]


def _fake_loop(monkeypatch, tmp_path, boards):
    """``run_forever`` with every collaborator faked except board selection, ``sync_all_once`` and the wait math.
    Returns the per-iteration rounds and the waits passed to ``sync_timing.wait``."""
    monkeypatch.chdir(tmp_path)
    rounds: list = [[]]
    waits: list = []
    monkeypatch.setattr(ksc, "load_identity", lambda: ksc.Identity("https://example.com", "test", "gateway"))
    monkeypatch.setattr(ksc, "board_paths", lambda: boards)
    monkeypatch.setattr(ksc.sync, "set_lease_client", lambda _: None)
    monkeypatch.setattr(ksc, "publish_pickup_if_changed", lambda _: None)
    monkeypatch.setattr(ksc, "reconcile_links", lambda *a, **kw: None)
    monkeypatch.setattr(ksc, "outbox_pending", lambda paths: False)
    clock = [100.0]
    monkeypatch.setattr(ksc, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda _: None))
    from hermes_cli import machine_metrics
    monkeypatch.setattr(machine_metrics, "Reporter",
                        lambda _: SimpleNamespace(report_due=lambda: None, pending_wake_in=lambda: None))

    class Thread:
        def __init__(self, **kw):
            pass

        def start(self):
            pass

        def is_alive(self):
            return True

    import threading
    monkeypatch.setattr(ksc, "threading", SimpleNamespace(Thread=Thread, Event=threading.Event))
    monkeypatch.setattr(ksc, "FeedWatcher", lambda *a, **kw: SimpleNamespace(run=lambda: None))

    def wait(fn, wake, paths, remaining, keep_running):
        waits.append(remaining)
        clock[0] += 1  # far less than the 30 s poll period: only a rerun selects a board
        rounds.append([])
        return "period"
    monkeypatch.setattr(ksc.sync_timing, "wait", wait)
    return rounds, waits


def test_the_loop_reruns_a_board_left_behind_without_the_poll_wait(monkeypatch, tmp_path):
    boards = [("one", Path("one")), ("two", Path("two"))]
    rounds, waits = _fake_loop(monkeypatch, tmp_path, boards)
    behind_rounds = {"one": 2}  # "one" stops at its page budget twice, then reaches the head

    def sync_board(slug, path, *args):
        rounds[-1].append(slug)
        left = behind_rounds.get(slug, 0)
        behind_rounds[slug] = max(0, left - 1)
        backlog.note(path, left > 0)
        return ksc.RoundResult(board=slug, mode="primary", behind=left > 0)
    monkeypatch.setattr(ksc, "sync_board", sync_board)
    ksc.run_forever(lambda: len(rounds) < 5, interval=30)
    assert rounds[:4] == [["one", "two"], ["one"], ["one"], []]
    assert waits[:3] == [0.0, 0.0, pytest.approx(28.0)]  # back on the 30 s period once the head is reached


def test_a_round_a_tick_holds_off_is_skipped_not_failed_and_retried_shortly(monkeypatch, tmp_path, caplog):
    """Review 5887313221 finding 2: a SyncRoundBusy round kept the previous round's behind flag, so the loop
    reran with zero wait and logged a WARNING per iteration while the dispatcher tick held the board; it also
    marked the board failed and silenced the stalled-outbox alarm (``_LAST_OK``)."""
    from hermes_cli import kanban_sync_work
    from hermes_cli.kanban_sync_guard import SyncRoundBusy
    boards = [("one", Path("one"))]
    rounds, waits = _fake_loop(monkeypatch, tmp_path, boards)
    finished: list = []
    real_finished = kanban_sync_work.SyncWork.finished
    monkeypatch.setattr(kanban_sync_work.SyncWork, "finished",
                        lambda self, board, ok, now=None: finished.append((board, ok)) or real_finished(self, board, ok, now))

    def sync_board(slug, path, *args):
        rounds[-1].append(slug)
        if len(rounds) == 1:  # the first round stops behind the head; then a tick holds the board
            backlog.note(path, True)
            return ksc.RoundResult(board=slug, mode="primary", behind=True)
        raise SyncRoundBusy("board sync round is busy; retry after it finishes")
    monkeypatch.setattr(ksc, "sync_board", sync_board)
    with caplog.at_level(logging.DEBUG, logger="hermes_cli.kanban_sync_client"):
        ksc.run_forever(lambda: len(rounds) < 6, interval=30)
    assert waits[0] == 0.0  # behind: rerun at once
    assert waits[1:5] == [ksc.BUSY_RETRY_S] * 4 and ksc.BUSY_RETRY_S > 0  # busy: a short wait, never a hot loop
    assert rounds[:5] == [["one"]] * 5  # the skipped board is retried every time
    assert finished == [("one", True)]  # never marked failed
    assert outbox_age._LAST_OK == {"one": True}  # the alarm keeps its last verdict
    busy = [r for r in caplog.records if "one" in r.getMessage() and "busy" in r.getMessage()]
    assert busy and {r.levelno for r in busy} == {logging.DEBUG}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# --- one replica puller per board per host --------------------------------------------------------------------

def test_a_second_replica_puller_leaves_the_cursor_alone_while_a_round_holds_the_board(live, svc, kanban_home):
    _other_creates(svc, 3)
    assert _round(kanban_home, svc.hub(ME)).error is None
    before, _ = _replica_state()
    _other_creates(svc, 5)
    with board_sync_lock(kanban_home / "kanban.db"):  # a round in progress
        pulled = ksc._pull_replica(BOARD, svc.hub(ME), ME, kanban_home / "kanban.db")
    assert pulled.busy and pulled == 0
    assert _replica_state()[0] == before
    pulled = ksc._pull_replica(BOARD, svc.hub(ME), ME, kanban_home / "kanban.db")  # free: it pulls
    assert not pulled.busy and _replica_state()[0] == svc.board(BOARD)["seq"]


def test_sync_diff_compares_the_replica_as_is_while_a_round_holds_the_board(live, svc, kanban_home, monkeypatch,
                                                                            capsys):
    import argparse
    import json
    from hermes_cli import kanban_sync_cli
    assert _round(kanban_home, svc.hub(ME)).error is None
    before, _ = _replica_state()
    _other_creates(svc, 4)
    monkeypatch.setattr(ksc, "load_identity", lambda: svc.hub(ME).ident)
    monkeypatch.setattr(ksc, "Hub", lambda ident: svc.hub(ME))
    with board_sync_lock(kanban_home / "kanban.db"):
        kanban_sync_cli._diff(argparse.Namespace(board=BOARD, json=True))
    report = json.loads(capsys.readouterr().out)
    assert "refresh_skipped" in report and not report.get("unavailable")
    assert _replica_state()[0] == before


def test_a_reconcile_under_a_backlog_lets_a_round_push_between_its_replica_chunks(live, svc, kanban_home,
                                                                                  monkeypatch):
    """Review 5887313221 nit 1: reconcile pulled the replica to the head in one locked pull, so under the incident's
    backlog every sync round (and its push) was refused for the whole catch-up. It now pulls in budgeted chunks and
    releases the board between them: a local edit made during the reconcile reaches the Board from a round in a
    gap, before the reconcile's replica reaches the head."""
    _other_creates(svc, BACKLOG_PAGES)
    log: list = []
    hub = PagedHub(svc, ME, log)
    edit: list = []
    real_changes = hub.changes

    def changes(board, since, limit=500):
        if not edit and since >= 2:  # a local edit while the reconcile is reading the backlog
            edit.append(kb.create_task(live, title="edited mid-reconcile"))
        return real_changes(board, since, limit)
    hub.changes = changes
    real_pull_replica = ksc._pull_replica
    gaps: list = []
    at_head: list = []

    def pull_replica(*a, **kw):  # the sync loop takes each gap the reconcile leaves (here: synchronously)
        pulled = real_pull_replica(*a, **kw)
        if pulled.more:
            gaps.append(_round(kanban_home, hub).error)
        else:
            at_head.append(len(log))  # the reconcile's replica reached the head
        return pulled
    monkeypatch.setattr(ksc, "_pull_replica", pull_replica)
    ksc.reconcile(BOARD, kanban_home / "kanban.db", hub=hub)
    assert edit and gaps and set(gaps) == {None}
    sent = [i for i, (kind, uids) in enumerate(log) if kind == "ops" and edit[0] in uids]
    assert sent and sent[0] < at_head[0], "the edit waited for the reconcile's whole catch-up"


def test_a_reconcile_waits_out_a_round_holding_the_board_then_gives_up(live, svc, kanban_home, monkeypatch):
    from hermes_cli.kanban_sync_guard import SyncRoundBusy
    naps: list = []
    monkeypatch.setattr(backlog, "_sleep", naps.append)
    monkeypatch.setattr(backlog, "RECONCILE_BUSY_WAIT_S", 2.0)
    with board_sync_lock(kanban_home / "kanban.db"), pytest.raises(SyncRoundBusy):
        backlog.replica_at_head(BOARD, svc.hub(ME), ME, kanban_home / "kanban.db")
    assert naps == [ksc.BUSY_RETRY_S] * 4


# --- the dispatcher tick: reservation is not held behind the backlog (scope addition 5/6) ---------------------

def _tick_setup(monkeypatch, hub):
    monkeypatch.setattr(ksc, "load_identity", lambda: ksc.Identity("http://unused", "synthetic", "hub"))
    monkeypatch.setattr(ksc, "Hub", lambda *a, **kw: hub)
    from hermes_cli import kanban_placement
    monkeypatch.setattr(kanban_placement, "config", lambda: {"worker_pools": {"hub": ["worker-1"]}})


def _backlog_pages(conn_, *, exhaust_after=None):
    """An endless backlog: every page has one change and ``more``; optionally the budget dies after N pages."""
    served = []

    def changes(board, since, limit=500):
        if exhaust_after is not None and len(served) >= exhaust_after:
            raise ksc.BoardBudgetExhausted("Board request budget exhausted")
        served.append(since)
        return {"changes": [], "next": since + 500, "head": 10 ** 9, "more": True}
    return changes, served


@pytest.mark.parametrize("exhaust_after", [1, None], ids=["budget-exhausted", "page-budget"])
def test_a_worker_first_card_is_reserved_on_the_first_tick_despite_a_backlog(conn, monkeypatch, exhaust_after):
    tid = reserve_fixtures.acknowledged(conn)
    hub = Mock()
    hub.changes, served = _backlog_pages(conn, exhaust_after=exhaust_after)
    hub.reserve.return_value = {"state": "assigned", "machine": "worker-1", "reservation_id": "r1",
                                "expires_at": None, "reason": None}
    _tick_setup(monkeypatch, hub)
    wp.dispatch_tick(conn)
    assert hub.reserve.call_count == 1
    assert hub.reserve.call_args.kwargs["task_id"] == tid
    assert len(served) <= 2 * backlog.PULL_PAGE_BUDGET  # the tick's pull and the post-reserve pull, each bounded
    assert len(served) < BACKLOG_PAGES


def test_a_raw_read_timeout_in_the_ticks_pull_stays_a_recorded_pull_error(conn, monkeypatch):
    """Review nit 2: only the shared budget running out keeps the tick going; a socket read timeout (a
    ``TimeoutError`` on the proxied urlopen path) is a pull failure with its diagnostics, as before."""
    reserve_fixtures.acknowledged(conn)
    hub = Mock()
    hub.changes.side_effect = TimeoutError("The read operation timed out")
    _tick_setup(monkeypatch, hub)
    from hermes_cli import kanban_queue_reason as queue
    recorded: list = []
    monkeypatch.setattr(queue, "try_record", lambda fn, *a, **kw: recorded.append((fn, kw.get("error"))))
    wp.dispatch_tick(conn)
    assert [(fn, type(err)) for fn, err in recorded] == [(queue.record_pull_error, TimeoutError)]
    assert hub.reserve.call_count == 0


def test_the_tick_neither_pulls_nor_reads_the_feed_while_a_round_holds_the_board(conn, monkeypatch):
    reserve_fixtures.acknowledged(conn)
    hub = Mock()
    hub.reserve.return_value = {"state": "assigned", "machine": "worker-1"}
    _tick_setup(monkeypatch, hub)
    pulls = []
    monkeypatch.setattr(ksc, "pull", lambda *a, **kw: pulls.append(kw))
    with board_sync_lock(Path(sync._db_key(conn))):
        wp.dispatch_tick(conn)
    assert pulls == [] and hub.reserve.call_count == 1


# --- detection -----------------------------------------------------------------------------------------------

def _age_rows(c, *, minutes, retry_after=None):
    with kbc.write_txn(c):
        c.execute("UPDATE kanban_sync_outbox SET created_at = ?, retry_after = ?",
                  (int((time.time() - minutes * 60) * 1000), retry_after))


def test_run_rows_held_while_behind_the_head_do_not_age_the_alarm(live, kanban_home):
    """A round behind the head holds run rows on purpose (finding 1), possibly for a long catch-up: that is not a
    stalled outbox. The same row ages the alarm again once the board reaches the head."""
    path = kanban_home / "kanban.db"
    kb.create_task(live, title="claimed while asleep")
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_outbox SET table_name = 'task_runs'")
    _age_rows(live, minutes=30)
    backlog.note(path, True)
    assert outbox_age._age(path) is None
    backlog.note(path, False)
    assert outbox_age._age(path) == pytest.approx(30 * 60, abs=5)


def test_sync_status_prints_the_age_of_the_oldest_pending_op(live):
    assert ksc.status(live)["oldest_pending_age_s"] is None or live.execute(
        "SELECT COUNT(*) FROM kanban_sync_outbox WHERE state='pending'").fetchone()[0]
    kb.create_task(live, title="waiting")
    _age_rows(live, minutes=540)
    age = ksc.status(live)["oldest_pending_age_s"]
    assert 540 * 60 - 5 <= age <= 540 * 60 + 5


def test_a_good_round_with_an_old_unanswered_op_warns_once_per_window(live, kanban_home, caplog):
    path = kanban_home / "kanban.db"
    kb.create_task(live, title="stuck")
    _age_rows(live, minutes=11)
    ok = [ksc.RoundResult(board=BOARD, mode="primary")]
    clock = [1000.0]
    assert outbox_age.after_round(ok, [(BOARD, path)], mono=lambda: clock[0] - 600) == []
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_sync_outbox_age"):
        assert outbox_age.after_round(ok, [(BOARD, path)], mono=lambda: clock[0]) == [BOARD]
        clock[0] += 60
        assert outbox_age.after_round(ok, [(BOARD, path)], mono=lambda: clock[0]) == []  # rate-limited
        failed = [ksc.RoundResult(board=BOARD, mode="?", error="OSError: offline")]
        clock[0] += 900
        assert outbox_age.after_round(failed, [(BOARD, path)], mono=lambda: clock[0]) == []  # offline: no claim
    assert sum("waited" in r.message for r in caplog.records) == 1
    _age_rows(live, minutes=11, retry_after=int(time.time()) + 60)  # the Board deferred it: its own retry
    assert outbox_age.after_round(ok, [(BOARD, path)], mono=lambda: clock[0] + 9999) == []


def test_the_dispatcher_files_one_card_per_stalled_outbox_incident(live, kanban_home, monkeypatch):
    monkeypatch.setattr(outbox_age.replica_age, "check", lambda *a, **kw: [])
    path = kanban_home / "kanban.db"
    kb.create_task(live, title="stuck")
    _age_rows(live, minutes=30)
    boards = [(BOARD, path)]
    clock = [0.0]
    filed: list = []

    def file_fn(slug, detail):
        filed.append(detail)
        return f"t_card{len(filed)}"

    def tick():
        clock[0] += outbox_age.CHECK_EVERY_S
        return outbox_age.check(mono=lambda: clock[0], file_fn=file_fn, boards=boards)

    assert tick() == []  # no round has completed in this process: nothing known about the loop
    outbox_age.after_round([ksc.RoundResult(board=BOARD, mode="primary")], boards, mono=lambda: clock[0])
    for _ in range(9):
        assert tick() == []
    assert tick() == ["t_card1"]
    assert tick() == []  # the same incident files once
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")
    assert tick() == []  # answered: the incident ends
    kb.create_task(live, title="stuck again")
    _age_rows(live, minutes=30)
    for _ in range(10):
        assert tick() == []  # replacement identity gets its own awake observation
    assert tick() == ["t_card2"]
    assert "30 min" in filed[0]


def test_a_round_stuck_past_the_window_counts_as_a_live_loop(live, kanban_home):
    """The incident's own shape: a round that never ends (its last completed round had failed)."""
    path = kanban_home / "kanban.db"
    kb.create_task(live, title="stuck")
    _age_rows(live, minutes=30)
    boards = [(BOARD, path)]
    outbox_age.after_round([ksc.RoundResult(board=BOARD, mode="?", error="URLError")], boards)
    outbox_age.round_started(mono=lambda: 0.0)
    filed = []
    assert outbox_age.check(mono=lambda: 300.0, file_fn=lambda s, d: filed.append(d), boards=boards) == []
    outbox_age.reset()
    outbox_age.round_started(mono=lambda: 0.0)
    outbox_age.check(mono=lambda: 900.0, file_fn=lambda s, d: filed.append(d) or "t_x",
                     boards=boards)
    assert len(filed) == 1


def test_report_opens_one_card_then_comments(live, kanban_home):
    first = outbox_age.report(BOARD, "board `default`: 30 min")
    again = outbox_age.report(BOARD, "board `default`: 40 min")
    assert first and first == again
    with kbc.connect_closing(kanban_home / "kanban.db") as c:
        row = c.execute("SELECT title, idempotency_key FROM tasks WHERE id = ?", (first,)).fetchone()
    assert row[1] == f"{outbox_age.KEY}:{BOARD}" and "outbox" in row[0]
