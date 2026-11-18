"""Online stale-FTS recovery (hermes_state_fts_online).

Hub, 2026-11-18: the trigram index of state.db had pages referenced twice, so DROP / 'delete-all' / 'rebuild'
all raised "database disk image is malformed", and the one-shot rebuild also waited forever for supervised
services that always hold the file. Search stayed on the LIKE fallback. The online path unlinks the indexes
without reading them, recreates them empty in one short transaction, and backfills in chunks while other
connections keep writing.
"""

import argparse
import sqlite3
import time

import pytest

from hermes_state import SessionDB
from hermes_state_common import FTS_REBUILD_DEFERRAL_KEY, FTS_STALE_KEY, _FTS_TRIGGERS
from hermes_state_fts_online import online_recovery_script
from hermes_state_repair import integrity_damage_is_structural

_OLD_WORDS = ["alphaword", "bravoword", "charlieword", "deltaword", "echoword"]


def _raw(path):
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _meta(path, key):
    with _raw(path) as conn:
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _seed(path, sessions=4, per=60):
    db = SessionDB(db_path=path)
    if not db._fts_enabled:
        db.close()
        pytest.skip("FTS5 unavailable in this build")
    for s in range(sessions):
        db.create_session(f"s{s}", source="test")
        for i in range(per):
            word = _OLD_WORDS[(s * per + i) % len(_OLD_WORDS)]
            db.append_message(f"s{s}", "user" if i % 2 == 0 else "assistant", f"{word} message {s}-{i} " + "x" * 40)
        db.append_message(f"s{s}", "tool", f"tool output {s} toolonlyword", tool_name="t")
    db.close()


def _break_trigram_btree(path):
    """The hub's failure class: the trigram index's b-tree is unreadable at the page level, so DROP / 'delete-all'
    / 'rebuild' raise "malformed" (corrupting its FTS blobs, as older tests do, leaves DROP working). Marks the
    store stale with triggers detached, as the live fail-open does."""
    conn = _raw(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'messages_fts_trigram_data'").fetchone()[0]
    page = conn.execute("PRAGMA page_size").fetchone()[0]
    for t in _FTS_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {t}")
    conn.execute("INSERT INTO state_meta (key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET value = '1'",
                 (FTS_STALE_KEY,))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    with open(path, "r+b") as fh:
        fh.seek((root - 1) * page)
        fh.write(b"\x0f" * 64)  # not a b-tree page type
    with _raw(path) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DROP TABLE messages_fts_trigram")


def _canonical(path):
    with _raw(path) as conn:
        return conn.execute("SELECT id, session_id, role, content FROM messages ORDER BY id").fetchall()


def _integrity_lines(path):
    with _raw(path) as conn:
        lines = [line for r in conn.execute("PRAGMA integrity_check(100000)") for line in str(r[0]).splitlines()]
        master = [tuple(r) for r in conn.execute("SELECT rootpage, type, name FROM sqlite_master WHERE rootpage > 0")]
    return lines, master


def _drain(db, limit_s=60.0):
    deadline = time.monotonic() + limit_s
    while db.advance_fts_backfill(budget_s=1.0):
        assert time.monotonic() < deadline, "backfill did not finish"


@pytest.fixture
def damaged(tmp_path):
    path = tmp_path / "state.db"
    _seed(path)
    before = _canonical(path)
    _break_trigram_btree(path)
    return path, before


def _open_stale(path):
    db = SessionDB(db_path=path)
    assert db._fts_stale is True  # the one-shot rebuild at open could not drop the damaged index
    db._fts_stale_retry_after = 0.0
    return db


def test_retry_tick_recovers_a_damaged_index_online_and_backfills(damaged):
    path, before = damaged
    db = _open_stale(path)
    try:
        assert db.retry_deferred_fts_recovery() is True
        assert db._fts_stale is False and db._fts_enabled is True
        assert _meta(path, FTS_STALE_KEY) is None and _meta(path, FTS_REBUILD_DEFERRAL_KEY) is None
        assert _meta(path, "fts_rebuild_high_water") == str(before[-1][0])
        with _raw(path) as conn:
            triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert set(_FTS_TRIGGERS) <= triggers

        # New messages are indexed by the triggers at once; old ones are found through the gap supplement.
        db.append_message("s0", "user", "zebracornword appended after recovery")
        assert db.search_messages("zebracornword")
        assert db.search_messages("charlieword")

        _drain(db)
        assert _meta(path, "fts_rebuild_high_water") is None and _meta(path, "fts_rebuild_progress") is None
        with _raw(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages_fts_docsize").fetchone()[0] == len(before) + 1
            eligible = conn.execute("SELECT COUNT(*) FROM messages WHERE role <> 'tool'").fetchone()[0]
            assert conn.execute("SELECT COUNT(*) FROM messages_fts_trigram_docsize").fetchone()[0] == eligible
            for table in ("messages_fts", "messages_fts_trigram"):
                conn.execute(f"INSERT INTO {table}({table}) VALUES('integrity-check')")
        assert db.search_messages("deltaword")
        assert db.search_messages("toolonlyword")  # base index carries tool rows
    finally:
        db.close()

    assert _canonical(path)[:len(before)] == before  # canonical rows untouched
    lines, master = _integrity_lines(path)
    leftovers = [l for l in lines if l not in ("ok", "*** in database main ***") and not l.endswith("never used")]
    assert leftovers == []  # only the unreachable old index pages remain
    assert integrity_damage_is_structural(lines, master) is False


def test_a_peer_keeps_writing_and_adopts_the_recovery(damaged):
    path, _before = damaged
    db = _open_stale(path)
    peer = _open_stale(path)
    try:
        assert db.retry_deferred_fts_recovery() is True
        # The peer's in-memory state is still stale; its tick adopts the recovery instead of rebuilding again.
        peer._recover_stale_fts = lambda *a, **k: pytest.fail("a peer must not redo the one-shot rebuild")
        assert peer.retry_deferred_fts_recovery() is True
        assert peer._fts_stale is False and peer._trigram_available is True
        for i in range(30):
            (db if i % 2 else peer).append_message("s1", "user", f"interleavedword {i}")
            (peer if i % 2 else db).advance_fts_backfill(budget_s=0.05)
        _drain(peer)
        _drain(db)
        with _raw(path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            assert conn.execute("SELECT COUNT(*) FROM messages_fts_docsize").fetchone()[0] == total
            eligible = conn.execute("SELECT COUNT(*) FROM messages WHERE role <> 'tool'").fetchone()[0]
            assert conn.execute("SELECT COUNT(*) FROM messages_fts_trigram_docsize").fetchone()[0] == eligible
        assert len(db.search_messages("interleavedword", limit=50)) == 30
    finally:
        peer.close()
        db.close()


def test_futile_holder_deferral_goes_online_without_a_failed_rebuild(tmp_path, monkeypatch):
    """Healthy but stale index, resident holders: the one-shot rebuild never runs; once the deferral is futile the
    next tick recovers online."""
    path = tmp_path / "state.db"
    _seed(path, sessions=2, per=10)
    with _raw(path) as conn:
        for t in _FTS_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {t}")
        conn.execute("INSERT INTO state_meta (key, value) VALUES (?, '1')", (FTS_STALE_KEY,))
    monkeypatch.setattr(SessionDB, "_foreign_state_db_holders", lambda self: [(424242, str(path))])
    db = SessionDB(db_path=path)
    try:
        assert db._fts_stale is True
        db._fts_stale_retry_after = 0.0
        assert db.retry_deferred_fts_recovery() is False  # deferred, not futile yet
        assert _meta(path, FTS_STALE_KEY) == "1"
        # The same holder for longer than the futility window (the record is recomputed on each deferral).
        with _raw(path) as conn:
            conn.execute("UPDATE state_meta SET value = json_set(value, '$.holders_since', ?, '$.holders_attempts', 50,"
                         " '$.first_seen', ?) WHERE key = ?", (time.time() - 7200, time.time() - 7200,
                                                               FTS_REBUILD_DEFERRAL_KEY))
        db._fts_stale_retry_after = 0.0
        db._fts_deferred_holder_pids = None
        assert db.retry_deferred_fts_recovery() is True
        assert _meta(path, FTS_STALE_KEY) is None
        _drain(db)
        assert db.search_messages("alphaword")
    finally:
        db.close()


def test_backfill_tick_respects_its_time_budget(tmp_path, monkeypatch):
    import hermes_state_fts_online

    path = tmp_path / "state.db"
    _seed(path, sessions=1, per=4)
    db = SessionDB(db_path=path)
    try:
        db.set_meta("fts_rebuild_high_water", "999999")
        db.set_meta("fts_rebuild_progress", "0")
        clock = [100.0]
        monkeypatch.setattr(hermes_state_fts_online.time, "monotonic", lambda: clock[0])
        calls = []

        def step():  # each chunk "costs" 0.25 s of the fake clock (exact in binary floating point)
            calls.append(1)
            clock[0] += 0.25
            return True

        monkeypatch.setattr(db, "fts_rebuild_step", step)
        assert db.advance_fts_backfill(budget_s=1.0) is True
        assert len(calls) == 4  # 1.0 s budget / 0.25 s per chunk
        assert db.advance_fts_backfill(budget_s=0) is True and len(calls) == 4  # no budget, no chunk
    finally:
        db.close()


def test_no_claim_no_work_and_refusals_change_nothing(damaged, monkeypatch):
    path, _before = damaged
    db = _open_stale(path)
    try:
        assert db.advance_fts_backfill() is False  # stale handle: nothing to advance
        monkeypatch.setattr(db, "_quarantine_reason", lambda: "structural corruption (test)")
        assert db.recover_stale_fts_online(reason="test") is False
        monkeypatch.undo()
        monkeypatch.setattr(db, "_db_has_legacy_inline_fts", lambda cursor: True)
        assert db.recover_stale_fts_online(reason="test") is False
        assert _meta(path, FTS_STALE_KEY) == "1"
        with _raw(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'messages_fts_trigram_data'"
                                ).fetchone()[0] == 1
    finally:
        db.close()
    ro = SessionDB(db_path=path, read_only=True)
    try:
        assert ro.recover_stale_fts_online(reason="test") is False
        assert ro.advance_fts_backfill() is False
    finally:
        ro.close()


def test_script_unlinks_only_the_fts_family(tmp_path):
    path = tmp_path / "state.db"
    _seed(path, sessions=1, per=4)
    with _raw(path) as conn:
        conn.execute("CREATE TABLE fts_v22_trash_messages_fts_data (id INTEGER PRIMARY KEY, block BLOB)")
        conn.execute("CREATE TABLE messages_ftsnot (x)")
        before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'messages\\_fts%' "
                                             "ESCAPE '\\' OR name = 'messages_ftsnot'")}
        family = "(name = 'messages_fts' OR name LIKE 'messages\\_fts\\_%' ESCAPE '\\')"
        roots = dict(conn.execute("SELECT name, rootpage FROM sqlite_master WHERE " + family))
        conn.executescript(online_recovery_script(trigram=True))
        after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'messages\\_fts%' "
                                            "ESCAPE '\\' OR name = 'messages_ftsnot'")}
        new_roots = dict(conn.execute("SELECT name, rootpage FROM sqlite_master WHERE " + family))
    assert after == before
    assert set(new_roots) == set(roots)  # same objects again ...
    assert all(new_roots[n] != roots[n] for n in new_roots if roots[n])  # ... on new pages


def test_cli_repair_search(damaged, capsys):
    from hermes_cli.sessions_cmd import _cmd_repair_search
    path, before = damaged
    db = _open_stale(path)
    try:
        assert _cmd_repair_search(db, argparse.Namespace(force=False, wait=True)) == 0
        out = capsys.readouterr().out
        assert "Search indexes recreated" in out and "Backfill complete" in out
        assert _meta(path, "fts_rebuild_high_water") is None
        assert _cmd_repair_search(db, argparse.Namespace(force=False, wait=False)) == 0
        assert "not stale" in capsys.readouterr().out
        assert db.search_messages("echoword")
    finally:
        db.close()
    assert _canonical(path)[:len(before)] == before


def test_gateway_housekeeping_tick_advances_the_backfill(damaged, monkeypatch):
    import contextlib

    import hermes_state_registry
    from gateway.run import _housekeeping_fts_backfill

    path, _before = damaged
    db = _open_stale(path)
    try:
        assert db.recover_stale_fts_online(reason="test") is True
        progress = _meta(path, "fts_rebuild_progress")

        @contextlib.contextmanager
        def borrow():
            yield [db]

        monkeypatch.setattr(hermes_state_registry, "borrow_live_shared_session_dbs", borrow)
        deadline = time.monotonic() + 60
        while _meta(path, "fts_rebuild_high_water") is not None:
            _housekeeping_fts_backfill()
            assert time.monotonic() < deadline
        assert progress == "0"
        assert db.search_messages("bravoword")
    finally:
        db.close()
