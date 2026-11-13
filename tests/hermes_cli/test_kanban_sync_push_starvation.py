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
    # 20 pages each for the replica and the live file, then the round ended anyway.
    assert first.behind and first.pulled == backlog.PULL_PAGE_BUDGET
    assert sum(1 for kind, _ in log if kind == "changes") == 2 * backlog.PULL_PAGE_BUDGET
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


def test_the_loop_reruns_a_board_left_behind_without_the_poll_wait(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    boards = [("one", Path("one")), ("two", Path("two"))]
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
    behind_rounds = {"one": 2}  # "one" stops at its page budget twice, then reaches the head

    def sync_board(slug, path, *args):
        rounds[-1].append(slug)
        left = behind_rounds.get(slug, 0)
        behind_rounds[slug] = max(0, left - 1)
        backlog.note(path, left > 0)
        return ksc.RoundResult(board=slug, mode="primary", behind=left > 0)
    monkeypatch.setattr(ksc, "sync_board", sync_board)

    def wait(fn, wake, paths, remaining, keep_running):
        waits.append(remaining)
        clock[0] += 1  # far less than the 30 s poll period: only a rerun selects a board
        rounds.append([])
        return "period"
    monkeypatch.setattr(ksc.sync_timing, "wait", wait)
    ksc.run_forever(lambda: len(rounds) < 5, interval=30)
    assert rounds[:4] == [["one", "two"], ["one"], ["one"], []]
    assert waits[:3] == [0.0, 0.0, pytest.approx(28.0)]  # back on the 30 s period once the head is reached


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


def test_the_dispatcher_files_one_card_per_stalled_outbox_incident(live, kanban_home):
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
    outbox_age.after_round([ksc.RoundResult(board=BOARD, mode="primary")], boards)
    assert tick() == ["t_card1"]
    assert tick() == []  # the same incident files once
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")
    assert tick() == []  # answered: the incident ends
    kb.create_task(live, title="stuck again")
    _age_rows(live, minutes=30)
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
    outbox_age.check(mono=lambda: outbox_age.STALE_S + 1, file_fn=lambda s, d: filed.append(d) or "t_x",
                     boards=boards)
    assert len(filed) == 1


def test_report_opens_one_card_then_comments(live, kanban_home):
    first = outbox_age.report(BOARD, "board `default`: 30 min")
    again = outbox_age.report(BOARD, "board `default`: 40 min")
    assert first and first == again
    with kbc.connect_closing(kanban_home / "kanban.db") as c:
        row = c.execute("SELECT title, idempotency_key FROM tasks WHERE id = ?", (first,)).fetchone()
    assert row[1] == f"{outbox_age.KEY}:{BOARD}" and "outbox" in row[0]
