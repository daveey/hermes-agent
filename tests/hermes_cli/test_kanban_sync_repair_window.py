"""A busy Board must not make every sync round re-run the full repair pass.

David-MBP, 2026-11-02 (t_01a199a0a5d38255): with no local work the gateway's sync thread used 13.5% of a core.
The replica's feed cursor is a repair input and moves on every change any gateway makes to the board, so the
default board ran the full repair pass (0.73 s thread-CPU each: ~6k rows decoded) on 29 rounds in 90 s. A change
that only moved the replica cursor now waits ``REPLICA_REPAIR_MIN_S`` after the last pass, and a write by
another process waits ``LOCAL_WRITE_REPAIR_MIN_S`` after it (hub 2026-11-13: 80 of 86 full passes in 601 s were
such writes); a rebuilt replica and the foreign-parent mirror are not delayed. [daveey]"""

from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_repair_window as win
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, OTHER, BOARD = "macbook", "mini", "default"
HEAVY = ("repair_partition_links", "repair_divergence", "repair_missing_children", "retire_lost_placeholder_links")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService(lease_runs=True)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ksc, "_repair_clock", lambda: now[0])
    return now


@pytest.fixture
def live(kanban_home, svc):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    ksc.reset_board_watch()
    previous = sync.set_lease_client(ksc.HubLeaseClient(svc.hub(ME)))
    try:
        yield conn
    finally:
        sync.set_lease_client(previous)
        ksc.reset_board_watch()
        conn.close()


def _round(kanban_home, svc):
    return ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)


def _spy(monkeypatch, names):
    import hermes_cli.kanban_sync_partition_repair as part
    calls = {n: 0 for n in names}
    for name in names:
        owner = part if hasattr(part, name) and not hasattr(ksc, name) else ksc
        real = getattr(owner, name)

        def wrapped(*a, _real=real, _name=name, **k):
            calls[_name] += 1
            return _real(*a, **k)
        monkeypatch.setattr(owner, name, wrapped)
    return calls


_SEQ = [0]


def _remote(svc, table="tasks", uid=None, fields=None):
    _SEQ[0] += 1
    uid = uid or f"t_remote{_SEQ[0]}"
    res = svc.hub(OTHER).ops(BOARD, [{
        "client_op_id": f"o-{_SEQ[0]}", "op": "upsert", "table": table, "uid": uid,
        "hlc": f"{time.time_ns() // 1_000_000 + 1000 + _SEQ[0]:013d}-0000-{OTHER}",
        "fields": fields or {"title": "remote", "status": "ready", "created_at": 1}}])
    assert res["results"][0]["status"] == "applied", res
    return uid


def _settle(live, svc, kanban_home, mode):
    sync.set_mode(live, mode)
    parent = kb.create_task(live, title="parent", assignee="worker")
    child = kb.create_task(live, title="child", assignee="worker")
    kb.link_tasks(live, parent, child)
    for _ in range(3):
        _round(kanban_home, svc)
    return parent, child


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_remote_changes_right_after_a_pass_do_not_rerun_it(live, svc, kanban_home, monkeypatch, mode):
    """Behaviour only (no new seams), so it also fails on the code before the fix: 5 scans there."""
    _settle(live, svc, kanban_home, mode)
    _round(kanban_home, svc)
    calls = _spy(monkeypatch, HEAVY)
    for _ in range(5):
        _remote(svc)
        _round(kanban_home, svc)
    assert all(n == 0 for n in calls.values()), f"each remote change re-ran the full repair pass: {calls}"


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_a_burst_of_remote_changes_runs_one_repair_pass_per_window(live, svc, kanban_home, monkeypatch, clock, mode):
    _settle(live, svc, kanban_home, mode)
    clock[0] += ksc.REPLICA_REPAIR_MIN_S + 1  # the settle passes are old: the next remote change repairs at once
    calls = _spy(monkeypatch, HEAVY)
    _remote(svc)
    assert not _round(kanban_home, svc).repair_deferred
    assert all(n == 1 for n in calls.values()), calls
    # Before the fix every one of these rounds re-ran the whole pass (the cursor moved each time).
    for _ in range(5):
        clock[0] += 1
        _remote(svc)
        assert _round(kanban_home, svc).repair_deferred
    assert all(n == 1 for n in calls.values()), f"remote-only changes re-ran the repair pass: {calls}"
    path = str(kanban_home / "kanban.db")
    assert ksc._REPAIR_DEFERRED[path] == pytest.approx(clock[0] - 5 + ksc.REPLICA_REPAIR_MIN_S)
    # The window opens: the sync loop selects the board and one pass covers the whole burst.
    boards = [(BOARD, kanban_home / "kanban.db")]
    assert ksc.deferred_repairs_due(boards, clock[0]) == []
    clock[0] += ksc.REPLICA_REPAIR_MIN_S
    assert ksc.deferred_repairs_due(boards, clock[0]) == boards
    assert not _round(kanban_home, svc).repair_deferred
    assert all(n == 2 for n in calls.values()), calls
    assert path not in ksc._REPAIR_DEFERRED
    _round(kanban_home, svc)
    assert all(n == 2 for n in calls.values()), f"an unchanged round repeated the pass: {calls}"


def _local_write(live, task, title):
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET title = ? WHERE id = ?", (title, task))


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_a_local_write_after_its_window_repairs_at_once(live, svc, kanban_home, monkeypatch, clock, mode):
    parent, _ = _settle(live, svc, kanban_home, mode)
    calls = _spy(monkeypatch, HEAVY)
    clock[0] += win.LOCAL_WRITE_REPAIR_MIN_S
    _local_write(live, parent, "renamed")
    assert not _round(kanban_home, svc).repair_deferred
    assert all(n == 1 for n in calls.values()), calls


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_a_burst_of_local_writes_runs_one_repair_pass_per_window(live, svc, kanban_home, monkeypatch, clock, mode):
    """Hub 2026-11-13: writes by other processes ran 80 full passes in 601 s on a 5,428-card board."""
    monkeypatch.setattr(win, "LOCAL_WRITE_REPAIR_MIN_S", 10.0)  # shorter than the feed window: told apart below
    # This case isolates WAL commits from main-file replacement/checkpoint
    # invalidation. Observe every opener, not only the fixture's writer.
    live.execute("PRAGMA wal_autocheckpoint=0")
    connect = kbc.connect
    def without_checkpoint(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.execute("PRAGMA wal_autocheckpoint=0")
        return conn
    monkeypatch.setattr(kbc, "connect", without_checkpoint)
    parent, _ = _settle(live, svc, kanban_home, mode)
    clock[0] += 60
    calls = _spy(monkeypatch, HEAVY)
    _local_write(live, parent, "w0")
    assert not _round(kanban_home, svc).repair_deferred
    assert all(n == 1 for n in calls.values()), calls
    passed_at = clock[0]
    path = str(kanban_home / "kanban.db")
    for i in range(5):
        clock[0] += 1
        _local_write(live, parent, f"w{i + 1}")
        if i == 3:
            _remote(svc)  # a feed change in the same burst must not push the due time out to the feed window
        assert _round(kanban_home, svc).repair_deferred
        assert ksc._REPAIR_DEFERRED[path] == pytest.approx(passed_at + 10.0)
    assert all(n == 1 for n in calls.values()), f"each local write re-ran the full repair pass: {calls}"
    boards = [(BOARD, kanban_home / "kanban.db")]
    assert ksc.deferred_repairs_due(boards, clock[0]) == []
    clock[0] = passed_at + 10.0
    assert ksc.deferred_repairs_due(boards, clock[0]) == boards
    assert not _round(kanban_home, svc).repair_deferred
    assert all(n == 2 for n in calls.values()), calls
    assert path not in ksc._REPAIR_DEFERRED
    # The pass cleared the credit: a feed-only round chains from it and defers on the feed window again.
    clock[0] += 1
    _remote(svc)
    assert _round(kanban_home, svc).repair_deferred
    assert ksc._REPAIR_DEFERRED[path] == pytest.approx(passed_at + 10.0 + ksc.REPLICA_REPAIR_MIN_S)


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_an_uncaptured_local_write_inside_the_window_is_healed(live, svc, kanban_home, clock, mode):
    """A capture gap re-installs the triggers, which moves the schema version: an ``inputs`` pass, never deferred."""
    parent, _ = _settle(live, svc, kanban_home, mode)
    _remote(svc)
    assert _round(kanban_home, svc).repair_deferred
    with kbc.write_txn(live):
        sync.drop_triggers(live)  # only the repair pass can find this write
        live.execute("UPDATE tasks SET title = 'gap edit' WHERE id = ?", (parent,))
    sync.install_triggers(live)
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    assert svc.board(BOARD)["rows"]["tasks"][parent]["title"] == "gap edit"


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_a_captured_local_write_is_pushed_by_a_deferred_round(live, svc, kanban_home, clock, mode):
    parent, _ = _settle(live, svc, kanban_home, mode)
    clock[0] += 1
    _local_write(live, parent, "pushed now")
    assert _round(kanban_home, svc).repair_deferred
    assert svc.board(BOARD)["rows"]["tasks"][parent]["title"] == "pushed now"


def test_a_rebuilt_replica_repairs_at_once(live, svc, kanban_home, monkeypatch, clock):
    _settle(live, svc, kanban_home, "shadow")
    calls = _spy(monkeypatch, HEAVY)
    _remote(svc)
    svc.compact(BOARD)  # the replica's cursor is now below the retention floor: the round rebuilds it
    # Never decided by inode alone: a delete-and-recreate can get the same inode back (ext4 does).
    real_stat = ksc.os.stat
    path = str(kanban_home / "kanban.db")
    _dev, ino = ksc._REPAIRED[path][1][:2]

    def _same_inode(st):
        fields = list(st[:10])
        fields[1], fields[2] = ino, _dev  # stat_result order: mode, ino, dev, ...
        return ksc.os.stat_result(fields)

    shadow_file = str(ksc.shadow_path(BOARD))
    monkeypatch.setattr(ksc.os, "stat", lambda p, *a, **k: _same_inode(real_stat(p)) if str(p) == shadow_file
                        else real_stat(p, *a, **k))
    assert not _round(kanban_home, svc).repair_deferred
    assert all(n == 1 for n in calls.values()), calls


@pytest.mark.parametrize("before,after", [("shadow", "primary"), ("primary", "shadow")])
def test_a_mode_change_inside_the_window_repairs_at_once(live, svc, kanban_home, monkeypatch, clock, before, after):
    _settle(live, svc, kanban_home, before)
    calls = _spy(monkeypatch, HEAVY)
    _remote(svc)
    assert _round(kanban_home, svc).repair_deferred
    sync.set_mode(live, after)
    _remote(svc)
    assert not _round(kanban_home, svc).repair_deferred
    assert all(n == 1 for n in calls.values()), calls


def test_a_foreign_parent_finishing_inside_the_window_releases_the_child_now(live, svc, kanban_home, clock):
    sync.set_mode(live, "primary")
    local_parent = kb.create_task(live, title="local parent", assignee="worker")
    child = kb.create_task(live, title="child", assignee="worker", parents=[local_parent])
    _round(kanban_home, svc)
    _remote(svc, uid="t_foreign", fields={"title": "foreign parent", "status": "ready", "created_at": 1})
    _remote(svc, table="task_links", uid=f"t_foreign>{child}", fields={"parent_id": "t_foreign", "child_id": child})
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    assert kb.claim_task(live, local_parent) is not None
    assert kb.complete_task(live, local_parent, result="ok")
    _round(kanban_home, svc)
    _round(kanban_home, svc)  # the previous round's push marked outbox rows: a live change, so it repairs
    kb.recompute_ready(live)
    assert getattr(kb.get_task(live, child), "status", None) == "todo"
    # The foreign parent finishes: a cursor-only change, so the heavy pass waits, but the mirror does not.
    _remote(svc, uid="t_foreign", fields={"status": "done"})
    sync.consume_dispatcher_wake()
    assert _round(kanban_home, svc).repair_deferred
    assert sync.consume_dispatcher_wake()
    kb.recompute_ready(live)
    assert getattr(kb.get_task(live, child), "status", None) == "ready"


def test_a_failed_round_drops_its_deferral(live, svc, kanban_home, monkeypatch, clock):
    _settle(live, svc, kanban_home, "shadow")
    _remote(svc)
    assert _round(kanban_home, svc).repair_deferred
    boards = [(BOARD, kanban_home / "kanban.db")]
    assert ksc.next_deferred_repair(boards) is not None

    def boom(*_a, **_k):
        raise OSError("disk gone")
    monkeypatch.setattr(ksc, "_pulled_replica", boom)
    with pytest.raises(OSError):
        _round(kanban_home, svc)
    # A stale due time would make the loop reselect the board with a zero wait, over and over.
    assert ksc.next_deferred_repair(boards) is None
    assert ksc.deferred_repairs_due(boards, clock[0] + 10 * ksc.REPLICA_REPAIR_MIN_S) == []


def test_the_sync_loop_runs_a_deferred_pass_when_it_is_due(monkeypatch, tmp_path):
    """run_forever: a due deferral selects its board even with no feed frame or poll, and caps the wait."""
    path = tmp_path / "kanban.db"
    boards = [(BOARD, path)]
    now = [500.0]
    monkeypatch.setattr(ksc, "_repair_clock", lambda: now[0])
    monkeypatch.setattr(ksc, "load_identity", lambda: ksc.Identity(url="https://board.invalid", key="k", gateway=ME))
    monkeypatch.setattr(ksc, "board_paths", lambda: boards)
    monkeypatch.setattr(ksc, "reconcile_links", lambda *a, **k: None)
    monkeypatch.setattr(ksc, "outbox_pending", lambda paths: False)
    monkeypatch.setattr(ksc.FeedWatcher, "run", lambda self: None)
    monkeypatch.setattr(sync, "set_lease_client", lambda client: None)
    import hermes_cli.machine_metrics as mm
    monkeypatch.setattr(mm.Reporter, "report_due", lambda self: None)
    import hermes_cli.kanban_sync_work as work_mod
    # No feed frame, poll or recovery is due in this window (the real select sets the next poll deadline).
    monkeypatch.setattr(work_mod.SyncWork, "select", lambda self, b, n, p: (setattr(self, "deadline", n + 60.0), [])[1])
    rounds, waits = [], []

    def fake_sync_all_once(hub, *, boards):
        rounds.append(list(boards))
        for _slug, p in boards:
            ksc._REPAIR_DEFERRED.pop(str(p), None)
        return []
    monkeypatch.setattr(ksc, "sync_all_once", fake_sync_all_once)

    def fake_wait(wake, paths, period, keep_running, tick=0.5):
        waits.append(period)
        now[0] += 7.0
        return "period"
    monkeypatch.setattr(ksc, "wait_for_work", fake_wait)
    ksc._REPAIR_DEFERRED[str(path)] = 510.0
    loops = iter(range(3))
    try:
        ksc.run_forever(lambda: next(loops, None) is not None, interval=60)
    finally:
        ksc.reset_board_watch()
    # Loop 1 at t=500: not due yet; the wait is capped at the 10 s left (not the 60 s poll period).
    # Loop 2 at t=507: still not due (3 s left). Loop 3 at t=514: due, so the board runs.
    assert rounds == [[], [], boards]
    assert waits[0] == pytest.approx(10.0) and waits[1] == pytest.approx(3.0)
