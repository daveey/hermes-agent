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
from hermes_state_fts_online import family_of, online_recovery_script
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


def _seed(path, sessions=4, per=150):
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


def _mark_fail_open(conn):
    """The live fail-open state: fts_stale set, FTS triggers detached."""
    for t in _FTS_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {t}")
    conn.execute("INSERT INTO state_meta (key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET value = '1'",
                 (FTS_STALE_KEY,))


def _break_trigram_btree(path, where="shared"):
    """``where="shared"``: the hub's damage shape. The trigram index's root page names one child page twice (and
    loses another), so integrity_check completes and reports "2nd reference to page", "out of order" and "never
    used" for that tree only. ``where="root"``: the root page is unreadable, so integrity_check itself fails."""
    import struct

    conn = _raw(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'messages_fts_trigram_data'").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    _mark_fail_open(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    with open(path, "r+b") as fh:
        fh.seek((root - 1) * page_size)
        page = bytearray(fh.read(page_size))
        if where == "root":
            page[:64] = b"\x0f" * 64
        else:
            assert page[0] == 5, "fixture needs a multi-page trigram index (interior table root)"
            first_cell = struct.unpack(">H", page[12:14])[0]
            page[8:12] = page[first_cell:first_cell + 4]  # right-most child := cell 0's child
        fh.seek((root - 1) * page_size)
        fh.write(page)
    with _raw(path) as conn:
        try:
            lines = [l for r in conn.execute("PRAGMA quick_check(50)") for l in str(r[0]).splitlines()]
        except sqlite3.DatabaseError:
            lines = None
    if where == "shared":
        assert any("2nd reference" in l for l in lines)


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
def damaged(tmp_path, monkeypatch):
    """The hub on 2026-11-18: a resident holder blocks the one-shot rebuild (and on this damage shape its DROP would
    free the shared page twice), the trigram index is damaged, search is stale."""
    path = tmp_path / "state.db"
    _seed(path)
    before = _canonical(path)
    _break_trigram_btree(path)
    monkeypatch.setattr(SessionDB, "_foreign_state_db_holders", lambda self: [(424242, str(path))])
    return path, before


def _open_stale(path):
    """Open, then age the holder deferral past the futility window (the record is recomputed on each deferral)."""
    db = SessionDB(db_path=path)
    assert db._fts_stale is True
    with _raw(path) as conn:
        conn.execute("UPDATE state_meta SET value = json_set(value, '$.holders_since', ?, '$.holders_attempts', 50, "
                     "'$.first_seen', ?) WHERE key = ?", (time.time() - 7200, time.time() - 7200,
                                                         FTS_REBUILD_DEFERRAL_KEY))
    db._fts_stale_retry_after = 0.0
    db._fts_deferred_holder_pids = None
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
        with _raw(path) as conn:  # the healthy base index went to trash and was drained and dropped
            assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'fts_v22_trash_%'").fetchone()[0] == 0
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
    # A healthy index is drained, never abandoned: the file is fully clean for every whole-file consumer.
    from hermes_cli.backup import verify_sqlite_integrity
    from hermes_cli.backup_sqlite import preflight_state_db
    from hermes_state_repair import _db_opens_cleanly

    lines, _master = _integrity_lines(path)
    assert lines == ["ok"]
    assert _db_opens_cleanly(path) is None
    assert verify_sqlite_integrity(path)["valid"] is True
    snapshot = preflight_state_db(path.parent)
    assert snapshot


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
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        conn.executescript(online_recovery_script(
            unlink_tables=[n for n in names if family_of(n)], trash=[], trash_tables=[], trigram=True, stamp=1))
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


def test_damage_outside_the_search_index_is_refused(tmp_path):
    path = tmp_path / "state.db"
    _seed(path, sessions=2, per=10)
    with _raw(path) as conn:
        for t in _FTS_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {t}")
        conn.execute("INSERT INTO state_meta (key, value) VALUES (?, '1')", (FTS_STALE_KEY,))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'sessions'").fetchone()[0]
        page = conn.execute("PRAGMA page_size").fetchone()[0]
    with open(path, "r+b") as fh:
        fh.seek((root - 1) * page)
        fh.write(b"\x0f" * 64)
    from hermes_state_fts_online import OnlineRecoveryRefused, plan_online_recovery
    with pytest.raises(OnlineRecoveryRefused):  # a full open would quarantine on the damaged canonical table first
        plan_online_recovery(path)
    with _raw(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'messages_fts_trigram_data'").fetchone()[0] == 1


@pytest.mark.parametrize("message,flag", [("database is locked", False), ("database disk image is malformed", True)])
def test_only_a_corrupt_image_marks_the_one_shot_as_failed(tmp_path, message, flag):
    path = tmp_path / "state.db"
    _seed(path, sessions=1, per=4)
    db = SessionDB(db_path=path)

    class Cursor:
        def execute(self, *_a, **_k):
            return self

        def fetchone(self):
            return None

        def executescript(self, _sql):
            raise sqlite3.OperationalError(message) if "locked" in message else sqlite3.DatabaseError(message)

    try:
        db._fts_oneshot_failed = False
        with db._lock:
            assert db._recover_stale_fts_locked(Cursor(), legacy=False) is False
        assert db._fts_oneshot_failed is flag
    finally:
        db.close()


def test_cli_wait_never_reports_success_it_did_not_reach(damaged, capsys, monkeypatch):
    from hermes_cli.sessions_cmd import _cmd_repair_search
    path, _before = damaged
    db = _open_stale(path)
    try:
        monkeypatch.setattr(db, "advance_fts_backfill", lambda budget_s=5.0: False)  # e.g. the handle went stale
        assert _cmd_repair_search(db, argparse.Namespace(force=False, wait=True)) == 1
        assert "stopped before finishing" in capsys.readouterr().out
    finally:
        db.close()


def test_an_index_damaged_so_badly_that_integrity_check_fails_is_refused(tmp_path, monkeypatch):
    """Root-page damage makes the whole-file check itself fail: without a complete map of which pages are shared,
    nothing may be unlinked or freed."""
    path = tmp_path / "state.db"
    _seed(path)
    _break_trigram_btree(path, where="root")
    monkeypatch.setattr(SessionDB, "_foreign_state_db_holders", lambda self: [(424242, str(path))])
    db = _open_stale(path)
    try:
        assert db.retry_deferred_fts_recovery() is False
        assert _meta(path, FTS_STALE_KEY) == "1"
        with _raw(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'messages_fts_trigram_data'"
                                ).fetchone()[0] == 1
    finally:
        db.close()


_MASTER = [(2, "table", "sessions"), (3, "table", "messages"), (80, "table", "messages_fts_trigram_data"),
           (81, "table", "messages_fts_trigram_idx"), (90, "table", "messages_fts_data")]


@pytest.mark.parametrize("lines,unlink", [
    (["Tree 80 page 2 cell 3: 2nd reference to page 7", "Page 9: never used"], ["messages_fts_trigram"]),
    (['fts5: corruption found reading blob 12 from table "messages_fts"'], []),  # page structure sound: drain
    (["Tree 80 page 2: x", "Tree 90 page 4: y"], ["messages_fts", "messages_fts_trigram"]),
    (["ok"], []),
])
def test_plan_unlinks_exactly_the_named_families(lines, unlink):
    from hermes_state_fts_online import plan_from_check
    plan = plan_from_check(lines, _MASTER)
    assert plan["unlink"] == unlink
    assert sorted(plan["unlink"] + plan["trash"]) == ["messages_fts", "messages_fts_trigram"]


@pytest.mark.parametrize("lines", [
    ["Tree 1 page 1 cell 3: 2nd reference to page 41"],          # the schema table itself
    ["Tree 99 page 5: 2nd reference to page 6"],                 # a root this snapshot cannot name
    ["Tree 3 page 7 cell 0: 2nd reference to page 80"],          # a canonical table
    ["Freelist: 2nd reference to page 5"],
    ["Tree 80 page 2: x"] * 100_000,                              # the check stopped at its limit
])
def test_plan_refuses_what_it_cannot_attribute(lines):
    from hermes_state_fts_online import OnlineRecoveryRefused, plan_from_check
    with pytest.raises(OnlineRecoveryRefused):
        plan_from_check(lines, _MASTER)


def test_family_of_full_matches_shadow_names():
    assert family_of("messages_fts_trigram_docsize") == "messages_fts_trigram"
    assert family_of("messages_fts_config") == "messages_fts"
    assert family_of("messages_fts_notes") is None and family_of("messages_ftsnot") is None
    assert family_of("fts_v22_trash_messages_fts_data_1") is None


def test_a_page_shared_with_a_canonical_table_is_never_freed(tmp_path, monkeypatch):
    """The trigram index points at a page of `messages`: the shared page is named against `messages`, which no index
    rebuild may touch."""
    import struct

    path = tmp_path / "state.db"
    _seed(path)
    conn = _raw(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'messages_fts_trigram_data'").fetchone()[0]
    victim = conn.execute("SELECT pageno FROM dbstat WHERE name = 'messages' AND pagetype = 'leaf' ORDER BY pageno "
                          "LIMIT 1").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    _mark_fail_open(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    with open(path, "r+b") as fh:
        fh.seek((root - 1) * page_size)
        page = bytearray(fh.read(page_size))
        assert page[0] == 5
        page[8:12] = struct.pack(">I", victim)
        fh.seek((root - 1) * page_size)
        fh.write(page)
    monkeypatch.setattr(SessionDB, "_foreign_state_db_holders", lambda self: [(424242, str(path))])
    db = _open_stale(path)
    try:
        # SQLite reaches the shared page from `messages` second and names Tree <messages>: damage outside the index,
        # so the recovery refuses and touches nothing (only `hermes sessions recover` may rebuild this store).
        assert db.retry_deferred_fts_recovery() is False
    finally:
        db.close()
    assert _meta(path, FTS_STALE_KEY) == "1"
    with _raw(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'messages_fts_trigram_data'"
                            ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'fts_v22_trash_%'").fetchone()[0] == 0


def test_one_shot_rebuild_never_drops_a_tree_that_shares_a_page(tmp_path):
    """No other holder, so the one-shot rebuild runs at open. On the hub's damage shape its DROP would succeed and free
    the shared page twice; it must step aside, and the retry recovers online instead."""
    path = tmp_path / "state.db"
    _seed(path)
    before = _canonical(path)
    _break_trigram_btree(path)
    db = SessionDB(db_path=path)
    try:
        assert db._fts_stale is True and db._fts_oneshot_failed is True
        with _raw(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'messages_fts_trigram_data'"
                                ).fetchone()[0] == 1  # not dropped
        db._fts_stale_retry_after = 0.0
        assert db.retry_deferred_fts_recovery() is True
        _drain(db)
        assert db.search_messages("alphaword")
    finally:
        db.close()
    assert _canonical(path)[:len(before)] == before
    lines, _master = _integrity_lines(path)
    assert not [l for l in lines if "2nd reference" in l or l.startswith("Freelist")]


def test_an_unnamed_family_that_shared_a_page_with_an_unlinked_one_drains_cleanly(tmp_path, monkeypatch):
    """The base index points at a trigram leaf: SQLite names only the base tree. Base is unlinked, trigram (unnamed)
    is trashed and drained, freeing the formerly shared page once; the file ends with no double reference."""
    import struct

    path = tmp_path / "state.db"
    _seed(path)
    before = _canonical(path)
    conn = _raw(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'messages_fts_data'").fetchone()[0]
    victim = conn.execute("SELECT pageno FROM dbstat WHERE name = 'messages_fts_trigram_data' AND pagetype = 'leaf' "
                          "ORDER BY pageno LIMIT 1").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    _mark_fail_open(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    with open(path, "r+b") as fh:
        fh.seek((root - 1) * page_size)
        page = bytearray(fh.read(page_size))
        assert page[0] == 5, "fixture needs a multi-page base index"
        page[8:12] = struct.pack(">I", victim)
        fh.seek((root - 1) * page_size)
        fh.write(page)
    from hermes_state_fts_online import plan_online_recovery
    plan = plan_online_recovery(path)
    monkeypatch.setattr(SessionDB, "_foreign_state_db_holders", lambda self: [(424242, str(path))])
    db = _open_stale(path)
    try:
        assert db.retry_deferred_fts_recovery() is True
        _drain(db)
        assert db.search_messages("bravoword")
    finally:
        db.close()
    assert "messages_fts" in plan["unlink"]
    assert _canonical(path)[:len(before)] == before
    lines, _master = _integrity_lines(path)
    assert not [l for l in lines if "2nd reference" in l or l.startswith(("Freelist", "Tree"))]


def test_a_refused_plan_is_remembered_by_the_one_shot_gate(tmp_path, monkeypatch):
    import hermes_state_fts_online

    path = tmp_path / "state.db"
    _seed(path, sessions=1, per=10)
    db = SessionDB(db_path=path)
    try:
        db.set_meta(FTS_STALE_KEY, "1")  # stale, so the online path does not just adopt a healthy store
        calls = []

        def refuse(db_path):
            calls.append(1)
            raise hermes_state_fts_online.OnlineRecoveryRefused("damage outside the index (test)")

        monkeypatch.setattr(hermes_state_fts_online, "plan_online_recovery", refuse)
        assert db._oneshot_drop_is_safe(legacy=False) is False
        assert db._oneshot_drop_is_safe(legacy=False) is False
        assert db.recover_stale_fts_online(reason="test") is False
        assert calls == [1]  # one whole-file check; the refusal is remembered for the gate and the online path
        assert db.recover_stale_fts_online(reason="operator", force=True) is False and calls == [1, 1]
    finally:
        db.close()


def test_retry_tick_plans_before_taking_the_session_lock(damaged, monkeypatch):
    import hermes_state_fts_online

    path, _before = damaged
    db = _open_stale(path)
    try:
        real = hermes_state_fts_online.plan_online_recovery
        held = []

        def plan(db_path):
            held.append(db._lock.locked())
            return real(db_path)

        monkeypatch.setattr(hermes_state_fts_online, "plan_online_recovery", plan)
        assert db.retry_deferred_fts_recovery() is True
        assert held == [False]  # one check, outside the lock (holders present: the online path plans for itself)
    finally:
        db.close()


def test_holder_leaving_mid_backoff_plans_outside_the_lock_and_never_reuses_an_old_plan(tmp_path, monkeypatch):
    """#106393's branch: the backoff has not elapsed but the holder set changed. The one-shot then runs; its gate gets a
    plan made this tick, outside the lock, never one from an earlier attempt."""
    import hermes_state_fts_online

    path = tmp_path / "state.db"
    _seed(path)
    before = _canonical(path)
    _break_trigram_btree(path)
    holders = [[(424242, str(path))]]
    monkeypatch.setattr(SessionDB, "_foreign_state_db_holders", lambda self: holders[0])
    db = SessionDB(db_path=path)
    try:
        assert db._fts_stale is True
        db._fts_stale_retry_after = time.monotonic() + 600
        db._fts_deferred_holder_pids = [424242]
        real = hermes_state_fts_online.plan_online_recovery
        held = []

        def plan(db_path):
            held.append(db._lock.locked())
            return real(db_path)

        monkeypatch.setattr(hermes_state_fts_online, "plan_online_recovery", plan)
        holders[0] = []  # the supervised holder stops
        # The gate refuses the DROP (named family), and the same tick recovers online with that plan.
        assert db.retry_deferred_fts_recovery() is True
        assert held == [False]
        assert db._fts_tick_plan is None  # discarded at the end of the tick
        _drain(db)
    finally:
        db.close()
    assert _canonical(path)[:len(before)] == before


def test_gate_never_plans_under_the_lock_inside_a_retry_tick(tmp_path, monkeypatch):
    import hermes_state_fts_online

    path = tmp_path / "state.db"
    _seed(path, sessions=1, per=10)
    db = SessionDB(db_path=path)
    try:
        monkeypatch.setattr(hermes_state_fts_online, "plan_online_recovery",
                            lambda p: pytest.fail("no whole-file check under the lock in a running process"))
        db._fts_in_retry_tick, db._fts_tick_plan = True, None
        with db._lock:
            assert db._oneshot_drop_is_safe(legacy=False) is False
    finally:
        db._fts_in_retry_tick = False
        db.close()

