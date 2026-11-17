"""Host-wide Board outages: one alarm card per host, and bounded round retries. [daveey]

Incident (2026-11-15 13:25-13:50 PST, David-MBP, t_01a1e1174026210f): DNS resolution of the Board failed
(``URLError: [Errno 8] nodename nor servname provided, or not known``) on every synced board. The sync loop retried
the failing rounds about every 0.6 s, and the replica-freshness alarm opened ten board-specific cards for one cause.

These tests drive the real entry paths: ``sync_all_once``/``sync_board`` on real synced board files against an
in-memory Board whose transport raises what ``urllib`` raises for a failed name lookup (and once the real ``Hub``
with ``getaddrinfo`` failing), the sync loop's ``after_round``, the dispatcher tick's ``outbox_age.check`` filing
real cards on the default board, and ``run_forever`` on a simulated clock.
"""
from __future__ import annotations

import errno
import itertools
import json
import socket
import sqlite3
import time as _time
import urllib.error
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_host_outage as host_outage
from hermes_cli import kanban_sync_outbox_age as outbox_age
from hermes_cli import kanban_sync_replica_age as replica_age
from hermes_cli.kanban_sync_work import FAIL_BACKOFF_MAX_S, SyncWork
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME = "macbook"
MIN = 60.0
DNS = "nodename nor servname provided, or not known"
TEN = ["default", "bb", "runners", "unconventional-collab", "goalfix-e2e", "softmax", "household", "metta",
       "paintbot", "coworld"]


def _dns_error() -> urllib.error.URLError:
    """What ``Hub`` raises for a failed name lookup (``urllib`` wraps the resolver's ``socket.gaierror``)."""
    return urllib.error.URLError(socket.gaierror(8, DNS))


class OutageHub:
    """The in-memory Board behind a transport that fails per board: ``down`` boards get the DNS failure,
    ``broken`` boards a Board-side 503 (not a host cause)."""

    def __init__(self, inner) -> None:
        self._inner, self.ident = inner, inner.ident
        self.down: set[str] = set()
        self.broken: set[str] = set()

    def report_metrics(self, *_a, **_k) -> None:  # the loop's Reporter is stubbed; nothing reads this
        return None

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            board = args[0] if args and isinstance(args[0], str) else kwargs.get("board")
            if "*" in self.down or board in self.down:
                raise _dns_error()
            if board in self.broken:
                raise ksc.HubError(503, "storage unavailable")
            return attr(*args, **kwargs)
        return call


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from cron import restart_rerun
    monkeypatch.setattr(restart_rerun, "host_fully_awake", lambda: True)
    outbox_age.reset()
    yield
    outbox_age.reset()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(h, True)
    monkeypatch.setattr(replica_age, "_core_sha", lambda: "2cc7101849aa")
    monkeypatch.setattr(ksc, "load_identity", lambda: (_ for _ in ()).throw(ksc.SyncUnavailable("no Board in tests")))
    return h


def _board(home, slug: str):
    path = home / "kanban.db" if slug == "default" else kb.board_dir(slug) / "kanban.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = kbc.connect(path)
    try:
        ksc.ensure_identity(conn, slug, ME)
        sync.set_mode(conn, "primary")
    finally:
        conn.close()
    return slug, path


def _write(path, title: str) -> str:
    conn = kbc.connect(path)
    try:
        return kb.create_task(conn, title=title)
    finally:
        conn.close()


def _pending(path) -> int:
    with kbc.connect_closing(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state = 'pending'").fetchone()[0]


def _cards(home) -> list[sqlite3.Row]:
    with kbc.connect_closing(home / "kanban.db") as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT id, title, body, status FROM tasks WHERE created_by = ? ORDER BY created_at, "
                            "rowid", (replica_age.AUTHOR,)).fetchall()


def _comments(home, tid: str) -> list[str]:
    with kbc.connect_closing(home / "kanban.db") as conn:
        return [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (tid,))]


class Host:
    """One gateway: a sync round (the real ``sync_all_once`` plus the loop's ``after_round``) and a dispatcher
    tick (the real ``outbox_age.check``, which runs the replica check and files real cards) every awake minute."""

    def __init__(self, home, slugs, *, svc=None) -> None:
        self.home = home
        self.boards = [_board(home, s) for s in slugs]
        self.svc = svc or FakeService()
        self.hub = OutageHub(self.svc.hub(ME))
        self.mono = 10_000.0
        self.rounding = {s for s in slugs}  # boards the loop still runs rounds for (others: a dead loop)

    def minute(self) -> None:
        self.mono += MIN
        live = [b for b in self.boards if b[0] in self.rounding]
        results = ksc.sync_all_once(self.hub, boards=live) if live else []
        outbox_age.after_round(results, self.boards, mono=lambda: self.mono)
        outbox_age.check(mono=lambda: self.mono, boards=self.boards, awake_fn=lambda: True)

    def run(self, minutes: int) -> None:
        for _ in range(minutes):
            self.minute()


def _host_cards(home):
    return [c for c in _cards(home) if "cannot reach the Board" in c["title"]]


# --- one host cause, one card ---------------------------------------------------------------------------------------

def test_ten_boards_failing_on_one_dns_cause_file_one_card_naming_every_board_and_frozen_card(home):
    host = Host(home, TEN)
    child = _write(host.boards[1][1], "rework after review")  # bb: a card frozen behind a remote parent
    with kbc.connect_closing(host.boards[1][1]) as conn, kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))
        conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", ("t_remote_parent", child))
    host.run(1)  # every replica reaches the feed head
    host.hub.down.add("*")
    host.run(19)
    assert _cards(home) == []
    host.run(3)
    cards = _cards(home)
    assert len(cards) == 1, [c["title"] for c in cards]
    card = cards[0]
    assert "cannot reach the Board (dns)" in card["title"] and "10 board replica(s)" in card["title"]
    for slug in TEN:
        assert f"### `{slug}`" in card["body"]
    assert child in card["body"] and "t_remote_parent" in card["body"]
    assert f"[Errno 8] {DNS}" in card["body"]
    assert "does not know why name resolution" in card["body"]
    host.run(60)
    assert len(_cards(home)) == 1 and _comments(home, card["id"]) == []  # the same incident files once


def test_replicated_foreign_host_incident_is_not_adopted(home, monkeypatch):
    """Both hosts see the same replicated owner rows; only same-host reports may coalesce."""
    monkeypatch.setattr(host_outage.socket, "gethostname", lambda: "host-a")
    first = host_outage.report([("bb", "DNS failed", "t_frozen_a")], fresh=True, cause="dns")
    monkeypatch.setattr(host_outage.socket, "gethostname", lambda: "host-b")
    second = host_outage.report([("runners", "DNS failed", "t_frozen_b")], fresh=True, cause="dns")
    assert first and second and first != second
    assert len(_cards(home)) == 2
    assert _comments(home, first) == []
    again = host_outage.report([("metta", "DNS failed", "t_frozen_late")], fresh=False, cause="dns")
    assert again == second
    assert "t_frozen_late" in _comments(home, second)[0]
    assert _comments(home, first) == []


def test_an_unrelated_board_failure_keeps_its_own_card(home):
    host = Host(home, TEN + ["ledger"])
    host.run(1)
    host.hub.down |= set(TEN)
    host.hub.broken.add("ledger")  # a Board-side 503 on one board: not a host cause
    host.run(22)
    cards = _cards(home)
    assert len(cards) == 2, [c["title"] for c in cards]
    (shared,) = _host_cards(home)
    (own,) = [c for c in cards if c["id"] != shared["id"]]
    assert "`ledger`" not in shared["body"]
    assert own["title"] == "Kanban sync: ledger replica has not reached the feed head for 20+ awake min"
    assert "HubError: hub 503: storage unavailable" in own["body"]


def test_an_archived_board_is_not_judged(home):
    host = Host(home, ["default", "bb", "goalfix-e2e"])
    meta = kb.board_dir("goalfix-e2e") / "board.json"
    meta.write_text(json.dumps({"name": "goalfix-e2e", "archived": True}))
    host.run(1)
    host.hub.down.add("*")
    host.run(22)
    (card,) = _cards(home)
    assert "### `bb`" in card["body"] and "### `default`" in card["body"]
    assert "goalfix-e2e" not in card["body"] and "2 board replica(s)" in card["title"]
    memo = json.loads((kb.kanban_home() / "kanban" / ".replica-age.json").read_text())
    assert "goalfix-e2e" not in memo


def test_a_board_without_a_recent_round_keeps_its_own_bounded_card(home):
    """No round for a while (a dead loop): the last DNS failure is no longer evidence of the cause, so the board
    files its own card on time instead of waiting to be classified."""
    host = Host(home, ["default", "bb"])
    host.run(1)
    host.hub.down.add("*")
    host.run(2)
    host.rounding.discard("bb")  # bb's rounds stop; default keeps failing on DNS
    host.run(20)
    titles = sorted(c["title"] for c in _cards(home))
    assert len(titles) == 2, titles
    assert any("bb replica has not reached the feed head" in t for t in titles)
    (shared,) = _host_cards(home)
    assert "### `default`" in shared["body"] and "`bb`" not in shared["body"]


def test_unclassified_failures_on_every_board_stay_per_board_and_bounded(home):
    host = Host(home, ["default", "bb"])
    host.run(1)
    host.hub.broken |= {"default", "bb"}
    host.run(19)
    assert _cards(home) == []
    host.run(2)
    assert sorted(c["title"].split(":")[1].split()[0] for c in _cards(home)) == ["bb", "default"]
    assert _host_cards(home) == []


def test_a_board_joining_later_is_named_on_the_same_card(home):
    host = Host(home, ["default", "bb", "runners"])
    host.run(1)
    host.hub.down |= {"default", "bb"}
    host.run(5)
    host.hub.down.add("runners")  # its replica went stale 5 min later
    host.run(17)
    (card,) = _cards(home)
    assert "### `runners`" not in card["body"]
    host.run(6)
    assert len(_cards(home)) == 1
    (comment,) = _comments(home, card["id"])
    assert comment.startswith("Also affected: ") and "### `runners`" in comment


def test_a_restart_mid_incident_keeps_the_same_host_card(home):
    host = Host(home, ["default", "bb"])
    host.run(1)
    host.hub.down.add("default")
    host.run(22)
    (card,) = _cards(home)
    outbox_age.reset()  # a new gateway process: in-memory state gone, .replica-age.json kept
    host.hub.down.add("bb")
    host.run(22)
    assert len(_cards(home)) == 1
    (comment,) = _comments(home, card["id"])
    assert comment.startswith("Also affected: ") and "`bb`" in comment


@pytest.mark.parametrize("closed", [False, True])
def test_recovery_then_recurrence_is_a_fresh_incident(home, closed):
    host = Host(home, ["default", "bb"])
    host.run(1)
    host.hub.down.add("*")
    host.run(22)
    (first,) = _cards(home)
    host.hub.down.clear()
    host.run(2)  # both replicas reach the head: the host incident ends
    assert replica_age._HOST_REC[0] is None
    if closed:
        with kbc.connect_closing(home / "kanban.db") as conn, kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (first["id"],))
    host.hub.down.add("*")
    host.run(22)
    cards = _cards(home)
    if closed:
        assert len(cards) == 2 and cards[1]["id"] != first["id"]
        assert "### `default`" in cards[1]["body"] and "### `bb`" in cards[1]["body"]
    else:
        assert len(cards) == 1
        (comment,) = _comments(home, first["id"])
        assert comment.startswith("New incident: ") and "`default`" in comment and "`bb`" in comment


# --- the real transport --------------------------------------------------------------------------------------------

def test_the_real_hub_classifies_a_failed_name_lookup_and_keeps_the_outbox(home, monkeypatch):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    slug, path = _board(home, "default")
    _write(path, "a local edit")
    before = _pending(path)
    assert before > 0

    def no_dns(*_a, **_k):
        raise socket.gaierror(8, DNS)

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    hub = ksc.Hub(ksc.Identity("https://board.example.invalid", "k", ME))
    (result,) = ksc.sync_all_once(hub, boards=[(slug, path)])
    assert result.cause == "dns" and DNS in result.error
    assert _pending(path) == before  # nothing dropped or rejected


@pytest.mark.parametrize("exc,want", [
    (urllib.error.URLError(socket.gaierror(8, DNS)), "dns"),
    (urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")), "network"),
    (urllib.error.URLError(OSError(errno.ENETUNREACH, "Network is unreachable")), "network"),
    (urllib.error.URLError(TimeoutError("timed out")), "network"),
    (urllib.error.URLError("no reason object"), None),
    (ksc.HubError(503, "storage unavailable"), None),
    (sqlite3.OperationalError("database is locked"), None),
    (OSError("hub unreachable"), None),
])
def test_classify(exc, want):
    assert host_outage.classify(exc) == want


# --- the sync loop: bounded retries, nothing lost, prompt recovery --------------------------------------------------

class Loop:
    """``run_forever`` with the real rounds and real ``wait_for_work`` on a simulated monotonic clock: the wait's
    ``wake.wait(timeout)`` advances it. Feed sockets are stubbed (disconnected, as in a DNS outage)."""

    def __init__(self, home, monkeypatch, slugs) -> None:
        self.clock = [100_000.0]
        self.boards = [_board(home, s) for s in slugs]
        self.svc = FakeService()
        self.hub = OutageHub(self.svc.hub(ME))
        self.rounds: dict[str, list[tuple[float, bool]]] = {s: [] for s in slugs}
        self.on_state: dict[str, Any] = {}
        self.hooks: list[tuple[float, Any]] = []
        self.restored = 0.0
        loop = self

        class Event:
            def __init__(self):
                self.flag = False

            def set(self):
                self.flag = True

            def clear(self):
                self.flag = False

            def is_set(self):
                return self.flag

            def wait(self, timeout=None):
                if not self.flag:
                    loop.clock[0] += max(0.0, timeout or 0.0)
                loop._fire()
                return self.flag

        class Watcher:
            def __init__(self, ident, board, wake, keep_running, **kwargs):
                loop.on_state[board] = kwargs.get("on_state")

            def run(self):
                pass

        class Thread:
            def __init__(self, target=None, **kwargs):
                self.target = target

            def start(self):
                self.target()

            def is_alive(self):
                return True

        real_round = ksc.sync_board

        def sync_board(slug, path, hub, gateway):
            try:
                result = real_round(slug, path, hub, gateway)
            except Exception:
                self.rounds[slug].append((self.clock[0], False))
                raise
            self.rounds[slug].append((self.clock[0], True))
            return result

        from hermes_cli import machine_metrics
        monkeypatch.setattr(machine_metrics, "Reporter",
                            lambda _: SimpleNamespace(report_due=lambda: None, pending_wake_in=lambda: None))
        monkeypatch.setattr(ksc, "load_identity", lambda: self.hub.ident)
        monkeypatch.setattr(ksc, "Hub", lambda ident: self.hub)
        monkeypatch.setattr(ksc, "board_paths", lambda: self.boards)
        monkeypatch.setattr(ksc.sync, "set_lease_client", lambda _: None)
        monkeypatch.setattr(ksc, "publish_pickup_if_changed", lambda _: None)
        monkeypatch.setattr(ksc, "reconcile_links", lambda *a, **k: None)
        clock = self.clock

        class FakeTime:  # the loop's module clock: simulated monotonic time, real wall time
            @staticmethod
            def monotonic():
                return clock[0]

            @staticmethod
            def sleep(_s):
                return None

            def __getattr__(self, name):
                return getattr(_time, name)

        monkeypatch.setattr(ksc, "time", FakeTime())
        monkeypatch.setattr(ksc, "threading", SimpleNamespace(Thread=Thread, Event=Event))
        monkeypatch.setattr(ksc, "FeedWatcher", Watcher)
        monkeypatch.setattr(ksc, "sync_board", sync_board)

    def at(self, offset: float, fn) -> None:
        self.hooks.append((self.clock[0] + offset, fn))

    def _fire(self) -> None:
        due = [h for h in self.hooks if h[0] <= self.clock[0]]
        self.hooks = [h for h in self.hooks if h[0] > self.clock[0]]
        for _at, fn in due:
            fn()

    def run(self, seconds: float, *, cap: int = 400) -> None:
        end = self.clock[0] + seconds
        ksc.run_forever(lambda: self.clock[0] < end and sum(map(len, self.rounds.values())) < cap, interval=5)

    def failed(self, slug: str) -> list[float]:
        return [t for t, ok in self.rounds[slug] if not ok]


def test_a_dns_outage_backs_rounds_off_keeps_the_outbox_and_recovers_promptly(home, monkeypatch):
    loop = Loop(home, monkeypatch, ["default", "bb"])
    for _slug, path in loop.boards:
        _write(path, "edit made during the outage")
    pending = {slug: _pending(path) for slug, path in loop.boards}
    loop.hub.down.add("*")
    start = loop.clock[0]

    def restore():
        loop.hub.down.clear()
        loop.restored = loop.clock[0]
        loop.on_state["default"]("default", True)  # default's feed socket reconnects; bb's does not

    loop.at(180.0, restore)
    loop.run(260.0)
    for slug, _path in loop.boards:
        fails = [t for t in loop.failed(slug) if t < start + 180.0]
        # 1, 2, 4 ... 60 s apart: about 8 attempts in 3 min, not one every 0.6 s (~300)
        assert 5 <= len(fails) <= 10, (slug, len(fails))
        gaps = [b - a for a, b in itertools.pairwise(fails)]
        assert all(g >= 1.0 for g in gaps) and max(gaps) <= FAIL_BACKOFF_MAX_S + 1.0, gaps
    by_slug = dict(loop.boards)
    # recovery: the reconnected board runs within a wait tick, the other within one backoff cap
    first_ok = {slug: min(t for t, ok in loop.rounds[slug] if ok and t >= loop.restored) for slug in by_slug}
    assert first_ok["default"] - loop.restored <= 1.0
    assert first_ok["bb"] - loop.restored <= FAIL_BACKOFF_MAX_S + 1.0
    for slug, path in loop.boards:
        assert pending[slug] > 0 and _pending(path) == 0  # every edit made during the outage reached the Board
        with kbc.connect_closing(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state = 'rejected'").fetchone()[0] == 0
        assert loop.svc.board(slug)["rows"].get("tasks")


def test_a_failing_board_never_slows_a_healthy_one(home, monkeypatch):
    loop = Loop(home, monkeypatch, ["default", "bb"])
    loop.hub.down.add("default")
    _write(dict(loop.boards)["default"], "stuck behind DNS")
    written: list[tuple[str, float]] = []

    def edit():
        written.append((_write(dict(loop.boards)["bb"], "healthy edit"), loop.clock[0]))

    for offset in (30.0, 61.5, 97.25):
        loop.at(offset, edit)
    loop.run(120.0)
    assert len(written) == 3
    for _tid, at in written:
        ok = [t for t, good in loop.rounds["bb"] if good and t >= at]
        assert ok and ok[0] - at <= 1.0  # pushed within a wait tick, as before
    assert _pending(dict(loop.boards)["bb"]) == 0
    assert len(loop.failed("default")) <= 9


def test_deferred_repair_during_post_pull_failure_does_not_spin(home, monkeypatch):
    loop = Loop(home, monkeypatch, ["default"])
    path = loop.boards[0][1]
    real_send, real_wait = ksc._send, ksc.wait_for_work
    waits, deferred = [], []
    failing = [True]

    def send(live, hub, slug, gateway, mode, result, **kwargs):
        if kwargs["after_pull"] and failing[0]:
            # A real post-pull transport failure leaves both pending writes and
            # any repair deferral created earlier in the round intact.
            _write(path, "post-pull edit")
            deferred.append(ksc._REPAIR_DEFERRED.get(str(path)))
            loop.hub.down.add(slug)
        try:
            return real_send(live, hub, slug, gateway, mode, result, **kwargs)
        finally:
            loop.hub.down.clear()

    def wait(wake, paths, timeout, keep_running):
        waits.append(timeout)
        assert len(waits) < 500, "deferred repair hot-spins during round backoff"
        return real_wait(wake, paths, timeout, keep_running)

    monkeypatch.setattr(ksc, "_send", send)
    monkeypatch.setattr(ksc, "wait_for_work", wait)
    start = loop.clock[0]
    loop.at(180, lambda: failing.__setitem__(0, False))
    loop.run(260)
    assert any(due is not None for due in deferred)
    assert loop.clock[0] >= start + 260
    assert len(loop.failed("default")) <= 10
    assert _pending(path) == 0
    assert any(ok and t >= start + 180 for t, ok in loop.rounds["default"])


def test_sync_work_backoff_keeps_dirty_flags_and_clears_on_success_or_reconnect():
    from pathlib import Path
    work = SyncWork(5)
    boards = [("a", Path("a")), ("b", Path("b"))]
    assert work.select(boards, 100, lambda _p: True) == boards  # first audit
    work.finished("a", False, 100)
    work.changed("a")
    assert work.select(boards, 100.5, lambda _p: True) == boards[1:]  # a backs off 1 s, b's write goes now
    assert work.select(boards, 101.0, lambda _p: False) == boards[:1]  # its kept dirty flag runs it after
    for streak, now in enumerate((101.0, 103.0, 107.0, 115.0, 131.0, 163.0, 227.0), start=2):
        work.finished("a", False, now)
        assert work.next_retry(now) == min(2 ** (streak - 1), FAIL_BACKOFF_MAX_S)
    assert work.backing_off("a", 286.0)
    work.feed_state("a", True)
    assert not work.backing_off("a", 286.0)
    work.finished("a", False, 300.0)
    assert work.next_retry(300.0) == FAIL_BACKOFF_MAX_S  # the streak carries until a round succeeds
    work.finished("a", True, 301.0)
    assert not work.backing_off("a", 301.0) and work.next_retry(301.0) is None
