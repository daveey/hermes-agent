"""A steady sync loop must not redo the chat-link reconcile (and its creator-chats backfill) when none of its inputs
changed, and must redo it the very next pass when one did. [daveey]

Gateway profile 2026-11-02 (task t_01a19d0bcbc52b4b): ``reconcile_links`` ran every 60 s unconditionally, re-reading
every task, subscription, ledger row and replica link of every board, and each placeholder check reloaded the
placement config (about 5k config loads per pass on a 600-card board). Its inputs are the live board's activity
revision, the replica's identity and feed cursor, this gateway's slice of the origin-ledger projection, the board
mode and the placement/gateway/profile config; state.db machine-session sources are bounded by a maximum age.
"""

from __future__ import annotations

import os
import sqlite3
import time

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_creator_chats as creator_chats
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_sync_client as ksc
from hermes_cli.kanban_sync_connections import connection_scope
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, OTHER, BOARD = "macbook", "mini", "default"
WORKER = "local-" + "7c" * 32  # a headless worker receipt session: its cards are creator-chats backfill candidates


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture
def ledger(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: list(rows))
    monkeypatch.setattr(ksa, "remote_links_and_origin_rows", lambda stale_ok=False: (list(rows), []))
    return rows


@pytest.fixture
def live(kanban_home, ledger):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    ksa._reset_cache_for_tests()
    try:
        yield conn
    finally:
        conn.close()
        ksa._reset_cache_for_tests()


@pytest.fixture
def scope():
    with connection_scope():
        yield


@pytest.fixture
def spies(monkeypatch):
    calls = {"reconcile": 0, "backfill": 0}
    real_reconcile, real_backfill = links.reconcile, creator_chats.backfill

    def reconcile(*a, **k):
        calls["reconcile"] += 1
        return real_reconcile(*a, **k)

    def backfill(*a, **k):
        calls["backfill"] += 1
        return real_backfill(*a, **k)

    monkeypatch.setattr(links, "reconcile", reconcile)
    monkeypatch.setattr(creator_chats, "backfill", backfill)
    return calls


def _ledger_row(task_id, session, *, released=False, board=BOARD):
    return {"id": task_id, "board": board, "gateway": OTHER, "key": f"{OTHER}/default/{board}/{task_id}",
            "session": session, "via": "shepherd", "origin_gateway": ME, "origin_profile": "default",
            "released": released, "status": "done" if released else "running", "unfinished": not released,
            "title": task_id}


def _links(conn):
    return {(r[0], r[1], r[2], r[3]) for r in conn.execute(
        "SELECT task_id, chat_session, kind, released FROM task_chat_links")}


def _pass(kanban_home, boards=None):
    ksc.reconcile_links(boards or [(BOARD, kanban_home / "kanban.db")], ME, skip_unchanged=True)


def _seed(live, ledger):
    creator = kb.create_task(live, title="creator", assignee="worker", session_id="s_creator")
    kb.create_task(live, title="worker child", assignee="worker", session_id=WORKER, creator_task_id=creator)
    orphan = kb.create_task(live, title="orphan", assignee="worker")
    kb.create_task(live, title="unresolved child", assignee="worker", session_id=WORKER, creator_task_id=orphan)
    ledger.append(_ledger_row("t_remote", "s_chat"))


def _settled(live, ledger, kanban_home, spies):
    _seed(live, ledger)
    for _ in range(3):  # backfill resolves a worker child once pass 1 wrote its creator's link; pass 3 is stable
        _pass(kanban_home)
    spies["reconcile"] = spies["backfill"] = 0
    return _links(live)


def test_unchanged_passes_skip_reconcile_and_backfill_with_the_same_links(live, ledger, kanban_home, spies, scope):
    settled = _settled(live, ledger, kanban_home, spies)
    assert ("t_remote", "s_chat", "shepherd", 0) in settled
    watch = sqlite3.connect(kanban_home / "kanban.db")
    try:
        version = watch.execute("PRAGMA data_version").fetchone()[0]
        for _ in range(4):
            _pass(kanban_home)
        assert watch.execute("PRAGMA data_version").fetchone()[0] == version, "an unchanged pass wrote the board"
    finally:
        watch.close()
    assert spies == {"reconcile": 0, "backfill": 0}
    assert _links(live) == settled


def test_a_live_board_change_reconciles_on_the_next_pass(live, ledger, kanban_home, spies, scope):
    _settled(live, ledger, kanban_home, spies)
    tid = kb.create_task(live, title="new", assignee="worker", session_id="s_new")
    _pass(kanban_home)
    assert spies["reconcile"] == 1 and spies["backfill"] == 1
    assert (tid, "s_new", "created", 0) in _links(live)


@pytest.mark.parametrize("before,after", [("shadow", "primary"), ("primary", "shadow")])
def test_a_mode_change_reconciles_on_the_next_pass(live, ledger, kanban_home, spies, scope, before, after):
    sync.set_mode(live, before)
    _settled(live, ledger, kanban_home, spies)
    _pass(kanban_home)
    assert spies == {"reconcile": 0, "backfill": 0}

    # Change only mode, not the tasks/links activity revision or the config signature.
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET mode = ?", (after,))
    assert sync.board_mode(live) == after
    _pass(kanban_home)
    assert spies == {"reconcile": 1, "backfill": 1}
    _pass(kanban_home)
    assert spies == {"reconcile": 1, "backfill": 1}


def test_a_subscription_change_reconciles_on_the_next_pass(live, ledger, kanban_home, spies, scope):
    """``kanban_notify_subs`` is outside the repair revision's tables; the reconcile key must still see it."""
    _settled(live, ledger, kanban_home, spies)
    tid = live.execute("SELECT id FROM tasks WHERE title = 'creator'").fetchone()[0]
    with kbc.write_txn(live):
        live.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at) "
                     "VALUES (?, 'tui', 's_watcher', 1)", (tid,))
    _pass(kanban_home)
    assert (tid, "s_watcher", "shepherd", 0) in _links(live)


def test_a_ledger_change_reconciles_on_the_next_pass(live, ledger, kanban_home, spies, scope):
    _settled(live, ledger, kanban_home, spies)
    ledger[:] = [_ledger_row("t_remote", "s_chat", released=True)]
    _pass(kanban_home)
    assert spies["reconcile"] == 1
    assert ("t_remote", "s_chat", "shepherd", 1) in _links(live)


def test_a_ledger_board_this_host_lacks_gets_its_link_only_file_on_the_next_pass(live, ledger, kanban_home, spies,
                                                                                  scope):
    _settled(live, ledger, kanban_home, spies)
    ledger.append(_ledger_row("t_run", "s_chat", board="runners"))
    _pass(kanban_home, ksc.board_paths())
    boards = dict(ksc.board_paths())
    assert boards["runners"] == kanban_home / "kanban" / sync.LINKS_DIR / "runners.db"
    conn = kbc.connect(boards["runners"])
    try:
        assert _links(conn) == {("t_run", "s_chat", "shepherd", 0)}
    finally:
        conn.close()


def test_a_placement_config_change_reconciles_on_the_next_pass(live, ledger, kanban_home, spies, scope):
    _settled(live, ledger, kanban_home, spies)
    assert ("t_remote", "s_chat", "shepherd", 0) in _links(live)
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  sync:\n    enabled: true\n  placement:\n    creator_session: s_chat\n")
    _pass(kanban_home)
    assert spies["reconcile"] == 1
    assert not any(r[1] == "s_chat" for r in _links(live)), "a configured placeholder session kept its link"


def test_a_replica_pull_reconciles_on_the_next_pass(live, ledger, svc, kanban_home, spies, scope):
    """An owner-written ``created`` link for this gateway's chat on a closed foreign card arrives only through the
    replica (its feed cursor moves); the chat's gateway must release it on the next pass."""
    path = kanban_home / "kanban.db"
    for _ in range(2):
        ksc.sync_board(BOARD, path, svc.hub(ME), ME)
    _settled(live, ledger, kanban_home, spies)
    hlc = f"{int(time.time() * 1000):013d}-0000-{OTHER}"
    uid = links.link_uid("t_theirs", ME, "default", "s1", "created")
    svc.hub(OTHER).ops(BOARD, [
        {"client_op_id": "o-t", "op": "upsert", "table": "tasks", "uid": "t_theirs", "hlc": hlc,
         "fields": {"title": "theirs", "status": "done", "created_at": 1}},
        {"client_op_id": "o-l", "op": "upsert", "table": "task_chat_links", "uid": uid, "hlc": hlc,
         "fields": {"task_id": "t_theirs", "chat_gateway": ME, "chat_profile": "default", "chat_session": "s1",
                    "kind": "created", "released": 0, "created_at": 1}}])
    _pass(kanban_home)
    assert spies["reconcile"] == 0, "nothing changed yet: the replica has not pulled"
    ksc.sync_board(BOARD, path, svc.hub(ME), ME)
    _pass(kanban_home)
    assert spies["reconcile"] == 1
    assert tuple(live.execute("SELECT released FROM task_chat_links WHERE uid = ?", (uid,)).fetchone()) == (1,)


def test_a_failed_pass_retries_on_the_next_pass(live, ledger, kanban_home, spies, scope, monkeypatch):
    _settled(live, ledger, kanban_home, spies)
    tid = kb.create_task(live, title="new", assignee="worker", session_id="s_new")
    spied = links.reconcile

    def boom(*a, **k):
        spies["reconcile"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(links, "reconcile", boom)
    _pass(kanban_home)
    monkeypatch.setattr(links, "reconcile", spied)
    _pass(kanban_home)
    assert spies["reconcile"] == 2, spies  # failed, then retried
    assert (tid, "s_new", "created", 0) in _links(live)
    spies["reconcile"] = 0
    _pass(kanban_home)
    _pass(kanban_home)
    assert spies["reconcile"] <= 1


def test_a_write_during_a_pass_is_not_cached_as_reconciled(live, ledger, kanban_home, spies, scope, monkeypatch):
    _settled(live, ledger, kanban_home, spies)
    spied = links.reconcile
    wrote: list[str] = []

    def concurrent(conn, *a, **k):
        out = spied(conn, *a, **k)
        if not wrote:
            other = kbc.connect(kanban_home / "kanban.db")
            try:
                wrote.append(kb.create_task(other, title="during", assignee="worker", session_id="s_during"))
            finally:
                other.close()
        return out

    monkeypatch.setattr(links, "reconcile", concurrent)
    ledger.append(_ledger_row("t_more", "s_chat"))  # arm one pass
    _pass(kanban_home)
    assert wrote and not any(r[0] == wrote[0] for r in _links(live))
    _pass(kanban_home)
    assert (wrote[0], "s_during", "created", 0) in _links(live)


def test_a_replaced_board_file_reconciles_on_the_next_pass(live, ledger, kanban_home, spies, scope):
    _settled(live, ledger, kanban_home, spies)
    path = kanban_home / "kanban.db"
    copy = path.with_name("restored.db")
    live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    backup = sqlite3.connect(copy)
    try:
        live.backup(backup)
        # A backup restarts the schema cookie; pin it so only the file generation tells the copies apart.
        backup.execute(f"PRAGMA schema_version = {live.execute('PRAGMA schema_version').fetchone()[0]}")
    finally:
        backup.close()
    inode = os.stat(path).st_ino
    os.replace(copy, path)  # same content and revision, new file generation
    assert os.stat(path).st_ino != inode
    _pass(kanban_home)
    assert spies["reconcile"] == 1


def test_the_maximum_age_reconciles_an_unchanged_board_whose_state_db_reads_failed(live, ledger, kanban_home, spies,
                                                                                   scope, monkeypatch):
    """State.db answers are keyed by value (``kanban_links_chat_key``); only a pass that could not read them is
    bounded by ``LINKS_MAX_AGE_S`` instead."""
    with sqlite3.connect(kanban_home / "state.db") as state:  # no sessions table: every source read fails
        state.execute("CREATE TABLE other (id TEXT)")
    state.close()
    _settled(live, ledger, kanban_home, spies)
    later = time.monotonic() + ksc.LINKS_MAX_AGE_S + 1
    monkeypatch.setattr(ksc, "_links_clock", lambda: later)
    _pass(kanban_home)
    assert spies["reconcile"] == 1


def test_an_explicit_reconcile_always_runs(live, ledger, kanban_home, spies, scope):
    """``hermes kanban sync links`` (no ``skip_unchanged``) reconciles now, whatever the loop last saw."""
    _settled(live, ledger, kanban_home, spies)
    for _ in range(2):
        ksc.reconcile_links([(BOARD, kanban_home / "kanban.db")], ME)
    assert spies["reconcile"] == 2


def test_the_loop_reuses_its_board_connection_across_passes(live, ledger, kanban_home, spies, scope, monkeypatch):
    _seed(live, ledger)
    opened = []
    real = ksc._connect
    monkeypatch.setattr(ksc, "_connect", lambda p: opened.append(p) or real(p))
    for _ in range(3):
        ksc.reconcile_links([(BOARD, kanban_home / "kanban.db")], ME)
    assert len(opened) == 1, opened


def _config_loads(monkeypatch):
    """Config loads made for the placement config (``kanban_placement.config``), not other readers'."""
    import sys
    from hermes_cli import config as hconfig
    from hermes_cli import kanban_placement
    count = {"n": 0}
    real = hconfig.load_config_readonly

    def counted():
        caller = sys._getframe(1)
        if caller.f_code is kanban_placement.config.__code__:
            count["n"] += 1
        return real()

    monkeypatch.setattr(hconfig, "load_config_readonly", counted)
    return count


def test_nested_config_snapshot_keeps_the_outer_pin(kanban_home, monkeypatch):
    from hermes_cli import kanban_placement as placement

    config_path = kanban_home / "config.yaml"
    config_path.write_text("kanban:\n  placement:\n    creator_session: s_before\n")
    count = _config_loads(monkeypatch)
    with placement.config_snapshot() as outer:
        assert outer["creator_session"] == "s_before"
        assert placement.config() is outer
        config_path.write_text("kanban:\n  placement:\n    creator_session: s_after\n")
        with placement.config_snapshot() as inner:
            assert inner is outer
            assert placement.config() is outer
            assert count["n"] == 1
        assert placement.config() is outer
        assert count["n"] == 1
    assert placement.config()["creator_session"] == "s_after"
    assert count["n"] == 2


def test_config_snapshot_pin_is_not_visible_to_another_thread(kanban_home, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from hermes_cli import kanban_placement as placement

    config_path = kanban_home / "config.yaml"
    config_path.write_text("kanban:\n  placement:\n    creator_session: s_before\n")
    count = _config_loads(monkeypatch)
    # This thread holds A's pin throughout B's read. Waiting on the future orders
    # both reads without sleeps and propagates any worker exception to pytest.
    with ThreadPoolExecutor(max_workers=1) as worker, placement.config_snapshot() as outer:
        assert outer["creator_session"] == "s_before"
        config_path.write_text("kanban:\n  placement:\n    creator_session: s_after\n")
        fresh = worker.submit(placement.config).result(timeout=10)
        assert fresh["creator_session"] == "s_after"
        assert fresh is not outer
        assert placement.config() is outer
        assert outer["creator_session"] == "s_before"
        assert count["n"] == 2


@pytest.mark.parametrize("cards", [3, 40])
def test_a_reconcile_pass_loads_the_placement_config_once(live, ledger, kanban_home, monkeypatch, scope, cards):
    """Placeholder checks run per task, subscription and link row; they share one placement snapshot per pass."""
    for i in range(cards):
        tid = kb.create_task(live, title=f"c{i}", assignee="worker", session_id=f"s{i}")
        with kbc.write_txn(live):
            live.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at) "
                         "VALUES (?, 'tui', ?, 1)", (tid, f"w{i}"))
        ledger.append(_ledger_row(f"t_r{i}", f"s{i}"))
    count = _config_loads(monkeypatch)
    ksc.reconcile_links([(BOARD, kanban_home / "kanban.db")], ME)
    assert count["n"] <= 1, count


@pytest.mark.parametrize("cards", [3, 40])
def test_a_repair_round_loads_the_placement_config_once(live, ledger, svc, kanban_home, monkeypatch, scope, cards):
    path = kanban_home / "kanban.db"
    for i in range(cards):
        kb.create_task(live, title=f"c{i}", assignee="worker", session_id=f"s{i}")
    ksc.sync_board(BOARD, path, svc.hub(ME), ME)
    kb.create_task(live, title="re-arm", assignee="worker")
    count = _config_loads(monkeypatch)
    ksc.sync_board(BOARD, path, svc.hub(ME), ME)
    assert count["n"] <= 1, count


def test_reset_forgets_reconciled_boards(live, ledger, kanban_home, spies, scope):
    _settled(live, ledger, kanban_home, spies)
    ksc.reset_board_watch()  # tests and a re-pointed home: no key survives into another file set
    _pass(kanban_home)
    assert spies["reconcile"] == 1


def test_the_sync_loop_skips_unchanged_links_passes(live, ledger, svc, kanban_home, spies, monkeypatch):
    """The loop's own entry path: ``run_forever`` reconciles links on its cadence, but an unchanged board only
    once, inside the loop's connection scope; a change between iterations reconciles on the next one."""
    from types import SimpleNamespace
    from hermes_cli import machine_metrics
    _seed(live, ledger)
    path = kanban_home / "kanban.db"
    iterations = {"n": 0}
    monkeypatch.setattr(ksc, "load_identity", lambda: SimpleNamespace(gateway=ME, url="fake", key="k"))
    hub = svc.hub(ME)
    monkeypatch.setattr(hub, "report_metrics", lambda *args: None, raising=False)  # Reporter construction only
    monkeypatch.setattr(ksc, "Hub", lambda ident: hub)
    monkeypatch.setattr(ksc.sync, "set_lease_client", lambda value: None)
    monkeypatch.setattr(ksc, "board_paths", lambda: [(BOARD, path)])
    monkeypatch.setattr(ksc, "FeedWatcher", lambda *a, **k: SimpleNamespace(run=lambda: None))
    monkeypatch.setattr(ksc, "sync_all_once", lambda *a, **k: [])
    monkeypatch.setattr(ksc, "LINKS_EVERY_S", 0.0)
    monkeypatch.setattr(machine_metrics, "Reporter", lambda *args: SimpleNamespace(report_due=lambda: None, pending_wake_in=lambda: None))
    per_iteration: list[int] = []

    def wait(*args):
        iterations["n"] += 1
        assert ksc._LINKS_RUNNER is not None and ksc._LINKS_RUNNER.wait_idle(30), "links pass did not finish"
        per_iteration.append(spies["reconcile"])

    monkeypatch.setattr(ksc, "wait_for_work", wait)
    ksc.run_forever(lambda: iterations["n"] < 6, interval=1)
    # Converged in three passes (backfill needs its creator's link first), then three unchanged iterations skipped.
    assert per_iteration == [1, 2, 3, 3, 3, 3], per_iteration
    tid = kb.create_task(live, title="between", assignee="worker", session_id="s_between")
    ksc.run_forever(lambda: iterations["n"] < 10, interval=1)
    assert (tid, "s_between", "created", 0) in _links(live)
    assert per_iteration[6:] == [4, 5, 5, 5], per_iteration  # the change, its own writes once, then skipped


@pytest.fixture
def slow_reconcile(monkeypatch):
    """Each board reconcile costs ``cost[0]`` seconds on a fake pass clock (t_01a1d35d8d7e22a7)."""
    now, cost = [1000.0], [0.0]
    real = links.reconcile

    def reconcile(*a, **k):
        now[0] += cost[0]
        return real(*a, **k)

    monkeypatch.setattr(ksc, "_links_pass_clock", lambda: now[0], raising=False)  # absent at base: fails on the line
    monkeypatch.setattr(links, "reconcile", reconcile)
    return cost


def _link_lines(caplog):
    return [r.getMessage() for r in caplog.records if "chat links" in r.getMessage()]


def test_the_links_log_line_carries_the_pass_duration(live, ledger, kanban_home, scope, slow_reconcile, caplog):
    """The hub's ~10 min pass (2026-11-13) logged only its counts, so no pass length could be read from the log.
    A pass with changes logs ``in <s>s``; an unchanged pass logs only at ``LINKS_SLOW_LOG_S`` (10 s) or more, with
    a whole-pass line naming the boards it covered."""
    caplog.set_level("INFO", logger="hermes_cli.kanban_sync_client")
    _seed(live, ledger)
    slow_reconcile[0] = 2.5
    _pass(kanban_home)
    changed = _link_lines(caplog)
    assert changed and changed[0].startswith(f"kanban sync: {BOARD} chat links {{") and changed[0].endswith(" in 2.5s")
    assert not any("pass over" in m for m in changed), "a 2.5 s pass is under the 10 s slow-pass line"

    for _ in range(3):
        ksc.reconcile_links([(BOARD, kanban_home / "kanban.db")], ME)
    caplog.clear()
    slow_reconcile[0] = 9.9  # unchanged, under the threshold: silent
    ksc.reconcile_links([(BOARD, kanban_home / "kanban.db")], ME)
    assert _link_lines(caplog) == []

    slow_reconcile[0] = 12.0  # unchanged but slow: logged with zero counts and its duration
    ksc.reconcile_links([(BOARD, kanban_home / "kanban.db")], ME)
    assert _link_lines(caplog) == [
        f"kanban sync: {BOARD} chat links {{'added': 0, 'changed': 0, 'released': 0, 'retired': 0}} in 12.0s",
        "kanban sync: chat links pass over 1 board(s) took 12.0s"]


def test_a_failed_board_logs_how_long_it_ran(live, ledger, kanban_home, scope, slow_reconcile, monkeypatch, caplog):
    """A slow failure (a lock or busy wait) is what the duration logging is for, so its warning carries it too."""
    caplog.set_level("INFO", logger="hermes_cli.kanban_sync_client")
    slow_reconcile[0] = 7.5
    timed = links.reconcile

    def boom(*a, **k):
        timed(*a, **k)  # spends the fake 7.5 s
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(links, "reconcile", boom)
    _pass(kanban_home)
    assert _link_lines(caplog) == [
        f"kanban sync: chat links for {BOARD} failed after 7.5s (OperationalError: database is locked)"]


def test_a_slow_unchanged_skip_names_its_board(live, ledger, kanban_home, scope, spies, monkeypatch, caplog):
    """A board whose inputs read is slow but unchanged is skipped; it still logs its name at LINKS_SLOW_LOG_S."""
    _settled(live, ledger, kanban_home, spies)
    now, cost = [1000.0], [0.0]
    real_inputs = ksc._links_inputs

    def slow_inputs(*a, **k):
        now[0] += cost[0]
        return real_inputs(*a, **k)

    monkeypatch.setattr(ksc, "_links_pass_clock", lambda: now[0])
    monkeypatch.setattr(ksc, "_links_inputs", slow_inputs)
    caplog.set_level("INFO", logger="hermes_cli.kanban_sync_client")
    caplog.clear()  # the settling passes' own lines
    cost[0] = 9.9  # under the threshold: silent
    _pass(kanban_home)
    assert _link_lines(caplog) == []
    cost[0] = 11.0
    _pass(kanban_home)
    assert spies["reconcile"] == 0, spies  # both passes skipped as unchanged
    assert _link_lines(caplog) == [f"kanban sync: {BOARD} chat links unchanged, skipped in 11.0s",
                                   "kanban sync: chat links pass over 1 board(s) took 11.0s"]
