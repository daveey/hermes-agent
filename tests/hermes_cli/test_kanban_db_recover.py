"""Automatic recovery of a Kanban board with table b-tree damage (hermes_cli/kanban_db_recover.py). [daveey]

Every test damages a real board file on purpose: one leaf page of ``task_events`` is overwritten, so the table
b-tree fails integrity_check (REINDEX cannot fix it) while the table's indexes still list the rows that were on
that page. That is the hub's 2026-10-28 incident shape: rows unreadable in the table, still named by indexes.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import struct
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_recover as rec

N_EVENTS = 1500


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _forget(path: Path) -> None:
    """Make this process re-probe ``path`` on its next connect (as a fresh process would)."""
    with kbc._INIT_LOCK:
        kbc._INITIALIZED_PATHS.discard(str(path.resolve()))


def _make_board(path: Path) -> list[str]:
    conn = kbc.connect(path)
    try:
        tids = [kb.create_task(conn, title=f"card {i}", assignee="default") for i in range(3)]
        with kbc.write_txn(conn):
            conn.executemany(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'note', ?, ?)",
                [(tids[i % 3], json.dumps({"i": i, "pad": "x" * 200}), 1_800_000_000 + i) for i in range(N_EVENTS)])
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    finally:
        conn.close()
    _forget(path)
    return tids


def _varint(buf: bytes, at: int) -> tuple[int, int]:
    value = 0
    for i in range(9):
        byte = buf[at + i]
        if i == 8:
            return (value << 8) | byte, at + 9
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, at + i + 1
    raise AssertionError("unreachable")


def _table_leaves(data: bytes, page_size: int, root: int) -> list[int]:
    """Leaf pages of a table b-tree, left to right (SQLite file format, section 1.6)."""
    off = (root - 1) * page_size
    hdr = off + (100 if root == 1 else 0)
    kind = data[hdr]
    if kind == 0x0D:
        return [root]
    assert kind == 0x05, f"page {root} is not a table b-tree page ({kind:#x})"
    ncells, = struct.unpack(">H", data[hdr + 3:hdr + 5])
    right, = struct.unpack(">I", data[hdr + 8:hdr + 12])
    children = []
    for i in range(ncells):
        ptr, = struct.unpack(">H", data[hdr + 12 + 2 * i:hdr + 14 + 2 * i])
        children.append(struct.unpack(">I", data[off + ptr:off + ptr + 4])[0])
    return [leaf for child in children + [right] for leaf in _table_leaves(data, page_size, child)]


def _leaf_rowids(data: bytes, page_size: int, page: int) -> list[int]:
    off = (page - 1) * page_size
    ncells, = struct.unpack(">H", data[off + 3:off + 5])
    out = []
    for i in range(ncells):
        ptr, = struct.unpack(">H", data[off + 8 + 2 * i:off + 10 + 2 * i])
        _size, at = _varint(data, off + ptr)
        rowid, _ = _varint(data, at)
        out.append(rowid)
    return out


def _damage_table(path: Path, table: str) -> list[int]:
    """Overwrite the cell area of a middle leaf page of ``table``; return the rowids it held."""
    conn = sqlite3.connect(path)
    root = conn.execute("SELECT rootpage FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    conn.close()
    data = path.read_bytes()
    page_size, = struct.unpack(">H", data[16:18])
    leaves = _table_leaves(data, page_size, root)
    assert len(leaves) >= 5, "the table must span several leaf pages"
    page = leaves[len(leaves) // 2]
    rowids = _leaf_rowids(data, page_size, page)
    off = (page - 1) * page_size
    with open(path, "r+b") as fh:
        fh.seek(off + 8)
        fh.write(b"\xff" * (page_size - 8))
    return rowids


def _payloads(path: Path) -> dict[int, str]:
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute("SELECT id, payload FROM task_events"))
    finally:
        conn.close()


def test_damaged_board_fails_closed_then_recovers_in_place_with_a_loss_report(home):
    path = home / "kanban.db"
    tids = _make_board(path)
    before = _payloads(path)
    top_id = max(before)
    lost = _damage_table(path, "task_events")
    assert lost
    with pytest.raises(kbc.KanbanDbCorruptError):  # the guard refuses table damage: REINDEX cannot fix it
        kbc.connect(path)
    _forget(path)
    inode = os.stat(path).st_ino
    # A connection another process would hold across the recovery: it must stay usable.
    holder = sqlite3.connect(path)
    holder.execute("SELECT count(*) FROM tasks").fetchone()

    result = rec.spawn_recovery(path, wait=True, extra_args=["--no-sync"])

    assert result["status"] == "recovered", result
    events = result["tables"]["task_events"]
    assert events["known_lost"] == len(lost) and events["possibly_lost"] == len(lost)
    assert {k[0] for k in events["lost_keys"]} == set(lost)
    assert result["tables"]["tasks"]["known_lost"] == 0
    assert os.stat(path).st_ino == inode, "the board must be written in place, never replaced"
    report = json.loads(Path(result["report_path"]).read_text())
    assert report["tables"]["task_events"]["known_lost"] == len(lost)
    assert Path(result["snapshot_path"]).exists()
    # The held connection sees the rebuilt board.
    assert holder.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    after = dict(holder.execute("SELECT id, payload FROM task_events"))
    assert {k: v for k, v in before.items() if k not in lost}.items() <= after.items()
    assert not set(lost) & set(after)
    holder.close()
    # A lost row's id is never handed out again.
    conn = kbc.connect(path)
    try:
        seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='task_events'").fetchone()[0]
        assert seq >= top_id
        assert {t.id for t in kb.list_tasks(conn)} >= set(tids)
        card = conn.execute("SELECT id, title, body, assignee FROM tasks WHERE created_by='kanban-recover'").fetchone()
        assert card is not None and result["card_id"] == card[0]
        assert f"Known lost rows: {len(lost)}" in card[2] and card[3] == "default"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_rows_the_damage_took_are_filled_from_the_newest_healthy_backup(home):
    path = home / "kanban.db"
    _make_board(path)
    before = _payloads(path)
    # What the connect-time guard leaves behind: a content-addressed copy taken while the board was still healthy.
    shutil.copy2(path, path.with_name(path.name + ".corrupt.0123456789abcdef.bak"))
    lost = _damage_table(path, "task_events")

    result = rec.recover(path, file_card=False, reconcile_sync=False)

    assert result.status == "recovered", result.reason
    events = result.tables["task_events"]
    assert events.filled_from_backup == len(lost) and events.known_lost == 0
    assert result.fill_source and result.fill_source.endswith(".corrupt.0123456789abcdef.bak")
    assert _payloads(path) == before


def test_fill_never_resurrects_rows_deleted_after_the_backup(home):
    """A backup row no index of the damaged file lists was deleted since: filling it would bring it back."""
    path = home / "kanban.db"
    _make_board(path)
    shutil.copy2(path, path.with_name(path.name + ".corrupt.0123456789abcdef.bak"))
    conn = sqlite3.connect(path)
    deleted = [r[0] for r in conn.execute("SELECT id FROM task_events ORDER BY id DESC LIMIT 40")]
    conn.execute(f"DELETE FROM task_events WHERE id IN ({','.join('?' * len(deleted))})", deleted)
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    conn.close()
    before = _payloads(path)
    lost = _damage_table(path, "task_events")
    assert not set(lost) & set(deleted)

    result = rec.recover(path, file_card=False, reconcile_sync=False)

    assert result.status == "recovered", result.reason
    assert result.tables["task_events"].filled_from_backup == len(lost)
    after = _payloads(path)
    assert not set(deleted) & set(after), "rows deleted after the backup came back"
    assert after == before


def test_a_write_during_the_rebuild_is_not_overwritten(home, monkeypatch):
    """A commit between snapshot and install changes data_version on the watch connection: rebuild again."""
    path = home / "kanban.db"
    _make_board(path)
    _damage_table(path, "task_events")
    real_build, calls = rec._build, []

    def build_then_write(*args, **kwargs):
        calls.append(1)
        real_build(*args, **kwargs)
        if len(calls) == 1:
            writer = sqlite3.connect(path)
            writer.execute("UPDATE tasks SET title = 'written mid-rebuild' WHERE rowid = 1")
            writer.commit()
            writer.close()

    monkeypatch.setattr(rec, "_build", build_then_write)
    result = rec.recover(path, file_card=False, reconcile_sync=False)

    assert result.status == "recovered", result.reason
    assert len(calls) == 2 and not result.concurrent_writes
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT title FROM tasks WHERE rowid = 1").fetchone()[0] == "written mid-rebuild"
    conn.close()


def test_damaged_snapshots_are_rotated(tmp_path):
    db = tmp_path / "kanban.db"
    for i in range(rec._SNAPSHOT_RETENTION + 3):
        (tmp_path / f"kanban.db.recover-20261028T0400{i:02d}Z.damaged.bak").write_bytes(b"x")
    rec._rotate_snapshots(db)
    kept = sorted(p.name for p in tmp_path.glob("kanban.db.recover-*.damaged.bak"))
    assert len(kept) == rec._SNAPSHOT_RETENTION and kept[-1].endswith(f"{rec._SNAPSHOT_RETENTION + 2:02d}Z.damaged.bak")


def test_a_second_recovery_of_the_same_board_is_refused_busy(home):
    path = home / "kanban.db"
    _make_board(path)
    _damage_table(path, "task_events")
    digest = path.read_bytes()
    with rec.recovery_lock(path) as held:
        assert held
        result = rec.recover(path, file_card=False, reconcile_sync=False)
    assert result.status == "busy" and str(os.getpid()) in result.reason
    assert path.read_bytes() == digest, "a refused recovery writes nothing"


def test_a_board_a_clean_process_reads_fine_is_left_untouched(home):
    """A caller stranded on deleted -wal/-shm files sees "corruption" that is not on disk: the child re-checks."""
    path = home / "kanban.db"
    _make_board(path)
    digest = path.read_bytes()
    result = rec.recover(path, file_card=False, reconcile_sync=False)
    assert result.status == "healthy"
    assert path.read_bytes() == digest
    assert not list(home.glob("kanban.db.recovery-*.json"))


def test_dry_run_reports_the_loss_and_writes_nothing(home):
    path = home / "kanban.db"
    _make_board(path)
    lost = _damage_table(path, "task_events")
    digest = path.read_bytes()
    result = rec.recover(path, file_card=False, reconcile_sync=False, dry_run=True)
    assert result.status == "dry_run" and result.tables["task_events"].known_lost == len(lost)
    assert path.read_bytes() == digest


def _dispatcher(kb_module):
    from gateway.kanban_watchers_dispatcher import _DispatcherSettings, _KanbanDispatcher
    settings = _DispatcherSettings(interval=60, max_spawn=None, max_in_progress=None, failure_limit=2,
                                   stale_timeout_seconds=0, reconcile_orphans=True, default_assignee=None,
                                   max_in_progress_per_profile=None)
    return _KanbanDispatcher(kb_module, settings)


@pytest.mark.parametrize("enabled", [True, False])
def test_dispatcher_starts_recovery_when_it_pauses_a_damaged_board(home, monkeypatch, enabled):
    path = home / "kanban.db"
    _make_board(path)
    _damage_table(path, "task_events")
    started = []
    monkeypatch.setattr(rec, "spawn_recovery", lambda p, **kw: started.append(Path(p)))
    monkeypatch.setattr(rec, "auto_recover_enabled", lambda: enabled)
    dispatcher = _dispatcher(kb)

    assert dispatcher.tick_once_for_board("default") is None

    assert "default" in dispatcher.disabled_corrupt_boards
    assert started == ([path.resolve()] if enabled else [])


def test_auto_recover_follows_config(home):
    assert rec.auto_recover_enabled() is True  # default on
    (home / "config.yaml").write_text("kanban:\n  auto_recover_corruption: false\n")
    assert rec.auto_recover_enabled() is False


def test_spawn_recovery_is_rate_limited_per_board(home, monkeypatch):
    calls = []

    class _Popen:
        def __init__(self, cmd, **kw):
            calls.append(cmd)

    monkeypatch.setattr(rec.subprocess, "Popen", _Popen)
    monkeypatch.setattr(rec, "_SPAWNED", {})
    path = home / "kanban.db"
    path.write_bytes(b"")
    rec.spawn_recovery(path)
    rec.spawn_recovery(path)
    assert len(calls) == 1 and calls[0][1:4] == ["-m", "hermes_cli.kanban_db_recover", "--db"]


def _run_kanban_cli(argv: list[str]) -> int:
    import argparse

    from hermes_cli import kanban as kc
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return kc.kanban_command(parser.parse_args(["kanban", *argv]))


def test_cli_recover_rebuilds_a_board_init_refuses(home, capsys):
    path = kb.kanban_db_path()
    _make_board(path)
    lost = _damage_table(path, "task_events")
    rc = _run_kanban_cli(["recover", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["status"] == "recovered"
    assert payload["tables"]["task_events"]["known_lost"] == len(lost)
    _forget(path)
    kbc.connect(path).close()  # the guard now opens it


def test_cli_recover_reports_busy_while_another_recovery_runs(home, capsys):
    path = kb.kanban_db_path()
    _make_board(path)
    _damage_table(path, "task_events")
    with rec.recovery_lock(path):
        rc = _run_kanban_cli(["recover"])
    assert rc == 1 and "busy" in capsys.readouterr().out


def test_quarantine_rotation_keeps_manual_and_recovery_copies(tmp_path):
    base = tmp_path / "kanban.db"
    manual = tmp_path / "kanban.db.corrupt.manual-20261028T035603Z.bak"
    manual.write_bytes(b"forensic")
    for i in range(kbc._CORRUPT_BACKUP_RETENTION + 5):
        (tmp_path / f"kanban.db.corrupt.{i:016x}.bak").write_bytes(b"x")
    kbc._prune_corrupt_backups(tmp_path, base.name)
    assert manual.exists(), "a quarantine burst must not delete a manual forensic copy"
    assert len(list(tmp_path.glob("kanban.db.corrupt.*.bak"))) == kbc._CORRUPT_BACKUP_RETENTION + 1
