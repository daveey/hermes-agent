"""Idle entry points avoid directory listings and replica decoding without gating time or edits."""
import contextlib
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.tui_gateway import test_change_watcher as watcher_tests
from tests.tui_gateway.test_kanban_remote_idle_read import NOW, _insert, _replica
from tests.tui_gateway.test_kanban_remote_arrival import CHAT, _card
from tui_gateway import kanban_remote_notices as krn
from tui_gateway import server

watcher_home = watcher_tests.watcher_home


@pytest.mark.parametrize("kind", ["pairing", "outbox"])
def test_idle_directory_not_relisted_but_member_edit_seen(watcher_home, monkeypatch, kind):
    home, _ = watcher_home
    root = home / ("pairing" if kind == "pairing" else "bot_relay/outbox")
    root.mkdir(parents=True)
    member = root / ("slack-pending.json" if kind == "pairing" else "notice.json")
    member.write_text("{}")
    old = time.time_ns() - 10_000_000_000
    os.utime(root, ns=(old, old))
    probe = server._pairing_sig if kind == "pairing" else server._bot_relay_outbox_sig
    before = probe()
    listed, original = [], os.scandir

    def listing(path):
        listed.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", listing)
    assert probe() == before
    assert listed == []
    stat = member.stat()
    os.utime(member, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert probe() != before
    assert listed == []
    added = root / ("other-approved.json" if kind == "pairing" else "next.json")
    added.write_text("[]")
    probe()
    assert root in listed


@pytest.mark.parametrize("mode", ["delete", "wal"])
def test_sweep_idle_does_not_read_replica_and_unloaded_chat_is_retried(tmp_path, monkeypatch, mode):
    replica = _replica(tmp_path, [_card("t_idle", CHAT, status="done", at=NOW)], mode)
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    opened = []

    @contextlib.contextmanager
    def session_db(profile, sid):
        opened.append(sid)
        yield None, None

    def poll(live):
        return krn.sweep_remote_notices(replica_path=replica, gateway="macbook", ledger=ledger,
                                        now=NOW + 60, live_ids=live, session_db=session_db)

    poll({CHAT})
    poll({CHAT})
    # Count actual snapshot opens, including the warm seq-only projection path.
    original = krn._ro
    reads = []
    monkeypatch.setattr(krn, "_ro", lambda *a: reads.append(1) or original(*a))
    poll({CHAT})
    assert reads == []
    poll(set())
    assert opened == [CHAT] and reads == []
    _insert(replica, _card("t_new", CHAT, status="done", at=NOW), 2)
    poll(set())
    assert reads and len(opened) == 3


def test_sweep_commit_during_read_is_not_cached(tmp_path, monkeypatch):
    # Concurrent commits require WAL while the real reader snapshot is held.
    replica = _replica(tmp_path, [], 'wal')
    krn._reset_for_tests()
    original = krn._task_rows

    def rows_then_commit(*args):
        rows = original(*args)
        _insert(replica, _card("t_race", CHAT, status="done", at=NOW), 1)
        return rows

    monkeypatch.setattr(krn, "_task_rows", rows_then_commit)
    assert krn._sweep_snapshot(replica, "macbook")[0] == []
    monkeypatch.setattr(krn, "_task_rows", original)
    assert krn._sweep_snapshot(replica, "macbook")[0][0][2]["id"] == "t_race"


def test_session_commit_after_wal_frames_is_seen(watcher_home):
    import sqlite3
    from tests.tui_gateway.test_change_watcher import _seed_store
    from tests.tui_gateway.test_kanban_remote_idle_read import _rewrite

    home, _ = watcher_home
    path = home / "state.db"
    _seed_store(path)
    with contextlib.closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE sessions SET title='before'")
        writer.commit()
        before = server._session_db_content_sig(path)
        shm = Path(f"{path}-shm")
        unpublished = shm.read_bytes()
        writer.execute("UPDATE sessions SET title='after'")
        writer.commit()
        published = shm.read_bytes()
        # Settled frame timestamps model a publication delayed beyond the racy window.
        old = time.time_ns() - 10_000_000_000
        for file in (path, Path(f"{path}-wal")):
            os.utime(file, ns=(old, old))
        _rewrite(shm, unpublished)
        try:
            assert server._session_db_content_sig(path) == before
        finally:
            _rewrite(shm, published)
        assert server._session_db_content_sig(path) != before


def test_sweep_grace_expires_without_replica_change(tmp_path, monkeypatch):
    replica = _replica(tmp_path, [_card("t_grace", CHAT, status="done", at=NOW)])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    opened = []

    @contextlib.contextmanager
    def session_db(profile, sid):
        opened.append(sid)
        yield None, None

    for now in (NOW, NOW + 31):
        krn.sweep_remote_notices(replica_path=replica, gateway="macbook", ledger=ledger, now=now,
                                session_db=session_db, waker=SimpleNamespace(grace_s=30))
    assert opened == [CHAT]
