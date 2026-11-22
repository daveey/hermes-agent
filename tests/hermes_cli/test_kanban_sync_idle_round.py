"""An idle sync round must cost nothing it does not need: no write to the live board, no repeat of repair scans
whose inputs did not change, no connection opened per outbox poll. Gateway profile 2026-10-31 (t_01a1955ae48ded35):
the Board sync loop was the gateway's main idle CPU (about 300 ms of repair scans per 5 s round, a no-op write
transaction per board per round, 10 connection opens every 0.5 s)."""

from __future__ import annotations

import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_lockguard
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, OTHER, BOARD = "macbook", "mini", "default"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    try:
        yield conn
    finally:
        conn.close()


def _round(kanban_home, svc):
    return ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)


def _version(watch: sqlite3.Connection) -> int:
    return watch.execute("PRAGMA data_version").fetchone()[0]


def _settle(live, svc, kanban_home, mode):
    sync.set_mode(live, mode)
    parent = kb.create_task(live, title="parent", assignee="worker")
    child = kb.create_task(live, title="child", assignee="worker")
    kb.link_tasks(live, parent, child)
    for _ in range(3):
        _round(kanban_home, svc)
    return parent, child


def _spy(monkeypatch, names):
    calls = {n: 0 for n in names}
    for name in names:
        real = getattr(ksc, name)

        def wrapped(*a, _real=real, _name=name, **k):
            calls[_name] += 1
            return _real(*a, **k)
        monkeypatch.setattr(ksc, name, wrapped)
    return calls


REPAIRS = ("repair_divergence", "repair_missing_children", "retire_lost_placeholder_links")


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_an_idle_round_commits_nothing_to_the_live_board(live, svc, kanban_home, mode):
    _settle(live, svc, kanban_home, mode)
    # A foreign parent edge whose child is not in this file stays a partition-repair candidate forever.
    _other = svc.hub(OTHER)
    hlc = f"{int(time.time() * 1000):013d}-0000-{OTHER}"
    _other.ops(BOARD, [
        {"client_op_id": "o-p", "op": "upsert", "table": "tasks", "uid": "t_foreign", "hlc": hlc,
         "fields": {"title": "foreign", "status": "ready", "created_at": 1}},
        {"client_op_id": "o-c", "op": "upsert", "table": "tasks", "uid": "t_foreign_child", "hlc": hlc,
         "fields": {"title": "foreign child", "status": "todo", "created_at": 1}},
        {"client_op_id": "o-l", "op": "upsert", "table": "task_links", "uid": "t_foreign>t_foreign_child", "hlc": hlc,
         "fields": {"parent_id": "t_foreign", "child_id": "t_foreign_child"}}])
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    watch = sqlite3.connect(kanban_home / "kanban.db")
    try:
        before = _version(watch)
        for _ in range(3):
            _round(kanban_home, svc)
        assert _version(watch) == before, "an idle sync round wrote to the live board"
    finally:
        watch.close()


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_repair_scans_run_once_per_change_not_every_round(live, svc, kanban_home, monkeypatch, mode):
    _settle(live, svc, kanban_home, mode)
    calls = _spy(monkeypatch, REPAIRS)
    for _ in range(4):
        _round(kanban_home, svc)
    assert max(calls.values()) <= 1, f"repairs re-ran with unchanged inputs: {calls}"

    # A local write (any connection) re-arms them.
    seen = dict(calls)
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET title = 'renamed' WHERE title = 'parent'")
    _round(kanban_home, svc)
    assert all(calls[n] == seen[n] + 1 for n in REPAIRS), calls

    # So does a remote change pulled into the replica (its cursor moves), once the replica-only repair window
    # since the last pass has passed (test_kanban_sync_repair_window.py covers changes inside the window).
    seen = dict(calls)
    for _ in range(3):
        _round(kanban_home, svc)
    settled = dict(calls)
    later = time.monotonic() + ksc.REPLICA_REPAIR_MIN_S + 1
    monkeypatch.setattr(ksc, "_repair_clock", lambda: later)
    svc.hub(OTHER).ops(BOARD, [{"client_op_id": "o-x", "op": "upsert", "table": "tasks", "uid": "t_remote",
                                "hlc": f"{int(time.time() * 1000):013d}-0000-{OTHER}",
                                "fields": {"title": "remote", "status": "ready", "created_at": 1}}])
    _round(kanban_home, svc)
    assert all(calls[n] == settled[n] + 1 for n in REPAIRS), calls


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_a_capture_gap_after_idle_rounds_is_still_healed(live, svc, kanban_home, mode):
    parent, _child = _settle(live, svc, kanban_home, mode)
    for _ in range(3):
        _round(kanban_home, svc)
    with kbc.write_txn(live):
        sync.drop_triggers(live)  # an uncaptured write: only the repair scans can find it
        live.execute("UPDATE tasks SET title = 'gap edit' WHERE id = ?", (parent,))
    sync.install_triggers(live)
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    assert svc.board(BOARD)["rows"]["tasks"][parent]["title"] == "gap edit"


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_dispatch_clock_commits_do_not_repeat_unchanged_repairs(live, svc, kanban_home, monkeypatch, mode):
    _settle(live, svc, kanban_home, mode)
    with kbc.write_txn(live):
        live.execute("CREATE TABLE kanban_dispatch_stamp (id INTEGER PRIMARY KEY, at INTEGER)")
    for _ in range(3):
        _round(kanban_home, svc)
    calls = _spy(monkeypatch, REPAIRS)
    for stamp in range(4):
        with kbc.write_txn(live):
            live.execute("INSERT OR REPLACE INTO kanban_dispatch_stamp VALUES (1, ?)", (stamp,))
        _round(kanban_home, svc)
    assert not any(calls.values()), f"dispatch bookkeeping repeated repair scans: {calls}"


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_repair_revision_rolls_back_and_tracks_other_connection(live, svc, kanban_home, monkeypatch, mode):
    parent, _ = _settle(live, svc, kanban_home, mode)
    for _ in range(3):
        _round(kanban_home, svc)
    calls = _spy(monkeypatch, REPAIRS)
    writer = sqlite3.connect(kanban_home / "kanban.db", isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE tasks SET title = 'rolled back' WHERE id = ?", (parent,))
        writer.rollback()
        _round(kanban_home, svc)
        assert not any(calls.values())
        writer.execute("UPDATE tasks SET title = 'committed' WHERE id = ?", (parent,))
        _round(kanban_home, svc)
        assert all(n == 1 for n in calls.values()), calls
    finally:
        writer.close()


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_write_during_repair_is_not_cached_as_already_repaired(live, svc, kanban_home, monkeypatch, mode):
    parent, _ = _settle(live, svc, kanban_home, mode)
    real = ksc.retire_lost_placeholder_links
    wrote = False

    def concurrent_write(*args):
        nonlocal wrote
        result = real(*args)
        if not wrote:
            wrote = True
            with kbc.write_txn(live):
                # Suppress capture without DDL: schema_version cannot mask a lost revision.
                live.execute("UPDATE kanban_sync_state SET applying = 1")
                live.execute("UPDATE tasks SET title = 'during repair' WHERE id = ?", (parent,))
                live.execute("UPDATE kanban_sync_state SET applying = 0")
        return result

    monkeypatch.setattr(ksc, "retire_lost_placeholder_links", concurrent_write)
    ksc._REPAIRED.clear()
    _round(kanban_home, svc)
    assert wrote
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    assert svc.board(BOARD)["rows"]["tasks"][parent]["title"] == "during repair"


def test_outbox_poll_keeps_one_connection_per_board_and_sees_new_work(live, kanban_home, monkeypatch):
    # Keep the commit-reuse half free of the auto-checkpoint page-count lottery: the one
    # checkpoint below is explicit.
    live.execute("PRAGMA wal_autocheckpoint=0")
    path = kanban_home / "kanban.db"
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")
    opened = []
    real = kanban_lockguard.connect
    monkeypatch.setattr(kanban_lockguard, "connect", lambda *a, **k: opened.append(a) or real(*a, **k))
    for _ in range(20):
        assert ksc.outbox_pending([path]) is False
    assert len(opened) == 1, f"outbox poll opened {len(opened)} connections"
    kb.create_task(live, title="new local work", assignee="worker")  # captured into the outbox by trigger
    assert ksc.outbox_pending([path]) is True
    assert len(opened) == 1
    # A checkpoint moves the main file; the wal-index proves it was one, so the kept
    # reader stays and still sees the work.
    checkpoint = tuple(live.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
    assert checkpoint[0] == 0
    assert ksc.outbox_pending([path]) is True
    assert len(opened) == 1
    for _ in range(20):
        assert ksc.outbox_pending([path]) is True
    assert len(opened) == 1


def test_outbox_poll_reopens_a_replaced_board_file(live, kanban_home):
    path = kanban_home / "kanban.db"
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")
    assert ksc.outbox_pending([path]) is False
    live.close()
    other = kanban_home / "other.db"
    conn = kbc.connect(other)
    try:
        ksc.ensure_identity(conn, BOARD, ME)
        kb.create_task(conn, title="pending here", assignee="worker")
    finally:
        conn.close()
    for suffix in ("", "-wal", "-shm"):
        p = kanban_home / f"kanban.db{suffix}"
        if p.exists():
            p.unlink()
    other.rename(path)
    for suffix in ("-wal", "-shm"):
        q = kanban_home / f"other.db{suffix}"
        if q.exists():
            q.rename(kanban_home / f"kanban.db{suffix}")
    assert ksc.outbox_pending([path]) is True
    assert ksc.outbox_pending([kanban_home / "missing.db"]) is False
