"""Every path that frees an FTS tree wholesale asks the whole-file plan first (hermes_state_fts_online).

Hub, 2026-11-18: a page of the trigram index's b-tree was referenced twice. DROP, FTS5 'rebuild' / 'delete-all'
and a teardown of the tree's rows free that page twice, and SQLite later hands it to canonical data. The online
recovery already refused to free such a tree; these tests cover the other paths: the open-time source realign,
trigram exclusion migration, orphan shadow sweep and missing-trigger rebuild, ``rebuild_fts()``, the
optimize-storage truncation and the trash teardown. On the hub's damage shape each gate leaves the tree alone and
detaches search (or, for damage it cannot attribute, changes nothing); the retry then recovers online and the file
ends with no double reference. On a healthy store each path behaves as before.
"""

import json
import struct

import pytest

import hermes_state_fts_online
import hermes_state_schema_memo
from hermes_state import SessionDB
from hermes_state_fts import base_source_view_is_stale
from hermes_state_common import FTS_STALE_KEY, FTS_TRIGRAM_SQL, SCHEMA_VERSION, _FTS_TRIGGERS
from tests.hermes_state.test_fts_online_recovery import _canonical, _drain, _integrity_lines, _meta, _raw, _seed

_TRIGRAM_TRIGGERS = [t for t in _FTS_TRIGGERS if "trigram" in t]


def _break(path, table):
    """The hub's damage shape in *table*'s b-tree (its root names one child twice), without touching FTS state."""
    with _raw(path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    with open(path, "r+b") as fh:
        fh.seek((root - 1) * page_size)
        page = bytearray(fh.read(page_size))
        assert page[0] == 5, "fixture needs a multi-page index (interior table root)"
        first_cell = struct.unpack(">H", page[12:14])[0]
        page[8:12] = page[first_cell:first_cell + 4]
        fh.seek((root - 1) * page_size)
        fh.write(page)
    assert any("2nd reference" in line for line in _integrity_lines(path)[0])


def _root(path, name):
    with _raw(path) as conn:
        row = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


def _assert_never_double_freed(path, before):
    """Canonical rows unchanged and no page reachable twice or both free and in use."""
    assert _canonical(path)[:len(before)] == before
    lines, _master = _integrity_lines(path)
    assert not [line for line in lines if "2nd reference" in line or line.startswith(("Freelist", "Tree"))]


def _recover(db, path, before):
    """The gateway retry tick recovers the detached store online; the backfill and teardown finish."""
    db._fts_stale_retry_after = 0.0
    assert db.retry_deferred_fts_recovery() is True
    _drain(db)
    assert db.search_messages("alphaword")
    db.close()
    _assert_never_double_freed(path, before)


def _assert_gate_fired(db, path, table, root):
    assert db._fts_stale is True and db._fts_oneshot_failed is True
    assert _meta(path, FTS_STALE_KEY) == "1"
    assert _root(path, table) == root, "the damaged tree was dropped or rebuilt"
    assert any("2nd reference" in line for line in _integrity_lines(path)[0]), "the damaged tree was freed"
    with _raw(path) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'messages_fts%'"
                                ).fetchone()


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "state.db"
    _seed(path)
    hermes_state_schema_memo.clear()
    return path, _canonical(path)


@pytest.fixture
def plans(monkeypatch):
    """Spy on the whole-file check: one entry per check, whether the watched handle's lock was held."""
    real = hermes_state_fts_online.plan_online_recovery
    watched, calls = {}, []

    def spy(db_path):
        db = watched.get("db")
        calls.append(db._lock.locked() if db is not None else None)
        return real(db_path)

    monkeypatch.setattr(hermes_state_fts_online, "plan_online_recovery", spy)
    return watched, calls


# ── 1. open-time realign of the base index source (DROP messages_fts + 'rebuild') ──

def _stale_source_view(path, *, claim):
    with _raw(path) as conn:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'messages_fts_src'").fetchone()[0]
        conn.execute("DROP VIEW messages_fts_src")
        conn.execute(sql + " AND 1 = 1" if " WHERE " in sql.upper() else sql + " WHERE 1 = 1")
        if claim:
            conn.execute("INSERT INTO state_meta (key, value) VALUES ('fts_rebuild_high_water', '1')")
            conn.execute("INSERT INTO state_meta (key, value) VALUES ('fts_rebuild_progress', '1')")


def test_realign_keeps_a_damaged_base_index(store, plans):
    path, before = store
    _break(path, "messages_fts_data")
    _stale_source_view(path, claim=False)
    root = _root(path, "messages_fts_data")
    db = SessionDB(db_path=path)
    _assert_gate_fired(db, path, "messages_fts_data", root)
    assert plans[1] == [None]  # one whole-file check for the whole open
    _recover(db, path, before)


def test_realign_with_a_claim_keeps_a_damaged_trigram_index(store):
    path, before = store
    _break(path, "messages_fts_trigram_data")
    _stale_source_view(path, claim=True)  # the claim makes the realign 'rebuild' the trigram index too
    root = _root(path, "messages_fts_trigram_data")
    db = SessionDB(db_path=path)
    _assert_gate_fired(db, path, "messages_fts_trigram_data", root)
    _recover(db, path, before)


def test_realign_on_a_healthy_store_runs_as_before(store, plans):
    path, _before = store
    _stale_source_view(path, claim=False)
    db = SessionDB(db_path=path)
    try:
        assert db._fts_stale is False and db._fts_enabled is True
        with _raw(path) as conn:  # dropped and rebuilt from the aligned view
            assert not base_source_view_is_stale(conn.cursor())
        assert db.search_messages("charlieword")
    finally:
        db.close()
    assert len(plans[1]) == 1


# ── 2. trigram source-exclusion migration (drop view/triggers + 'rebuild') ──

def _behind_v30(path):
    with _raw(path) as conn:
        conn.execute("UPDATE schema_version SET version = 29")


def _schema_version(path):
    with _raw(path) as conn:
        return conn.execute("SELECT version FROM schema_version").fetchone()[0]


def test_trigram_exclusion_migration_keeps_a_damaged_trigram_index(store):
    path, before = store
    _break(path, "messages_fts_trigram_data")
    _behind_v30(path)
    root = _root(path, "messages_fts_trigram_data")
    db = SessionDB(db_path=path)
    _assert_gate_fired(db, path, "messages_fts_trigram_data", root)
    assert _schema_version(path) == 29  # held back: a later open migrates the recovered index
    _recover(db, path, before)


def test_trigram_exclusion_migration_on_a_healthy_store_runs_as_before(store):
    path, _before = store
    _behind_v30(path)
    db = SessionDB(db_path=path)
    try:
        assert db._fts_stale is False and db._trigram_available is True
        assert _schema_version(path) == SCHEMA_VERSION
    finally:
        db.close()


# ── 3. orphan shadow-table sweep (DROP of shadow tables whose vtable row is gone) ──

def _orphan(path, family):
    with _raw(path) as conn:
        for t in _FTS_TRIGGERS:
            if t.startswith(family + "_") and (family == "messages_fts_trigram" or "trigram" not in t):
                conn.execute(f"DROP TRIGGER IF EXISTS {t}")
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("DELETE FROM sqlite_master WHERE name = ? AND sql LIKE 'CREATE VIRTUAL TABLE%'", (family,))
        version = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute(f"PRAGMA schema_version={version + 1}")
        conn.execute("PRAGMA writable_schema=OFF")


def test_orphan_sweep_keeps_damaged_orphan_shadows(store):
    path, before = store
    _break(path, "messages_fts_trigram_data")
    _orphan(path, "messages_fts_trigram")
    root = _root(path, "messages_fts_trigram_data")
    db = SessionDB(db_path=path)
    _assert_gate_fired(db, path, "messages_fts_trigram_data", root)
    _recover(db, path, before)


def test_orphan_sweep_on_a_healthy_store_runs_as_before(store):
    path, _before = store
    _orphan(path, "messages_fts_trigram")
    db = SessionDB(db_path=path)
    try:
        assert db._fts_stale is False and db._trigram_available is True
        with _raw(path) as conn:  # the orphans were dropped and the index recreated and rebuilt
            assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'messages_fts_trigram'").fetchone()
        assert db.search_messages("deltaword")
    finally:
        db.close()


def test_orphan_sweep_refused_changes_nothing(store, monkeypatch):
    path, _before = store
    _orphan(path, "messages_fts_trigram")
    root = _root(path, "messages_fts_trigram_data")

    def refuse(db_path):
        raise hermes_state_fts_online.OnlineRecoveryRefused("damage outside the index (test)")

    monkeypatch.setattr(hermes_state_fts_online, "plan_online_recovery", refuse)
    db = SessionDB(db_path=path)
    try:
        assert db._fts_enabled is False and db._fts_stale is False
        assert _meta(path, FTS_STALE_KEY) is None
        assert _root(path, "messages_fts_trigram_data") == root
    finally:
        db.close()


# ── 4. open-time 'rebuild' of an existing index whose triggers are missing ──

def _drop_triggers(path, names=_FTS_TRIGGERS):
    with _raw(path) as conn:
        for t in names:
            conn.execute(f"DROP TRIGGER IF EXISTS {t}")


@pytest.mark.parametrize("missing", [_FTS_TRIGGERS, _TRIGRAM_TRIGGERS], ids=["base", "trigram"])
def test_missing_trigger_rebuild_keeps_a_damaged_index(store, missing):
    path, before = store
    _break(path, "messages_fts_trigram_data")
    _drop_triggers(path, missing)
    root = _root(path, "messages_fts_trigram_data")
    db = SessionDB(db_path=path)
    _assert_gate_fired(db, path, "messages_fts_trigram_data", root)
    _recover(db, path, before)


@pytest.mark.parametrize("missing", [_FTS_TRIGGERS, _TRIGRAM_TRIGGERS], ids=["base", "trigram"])
def test_missing_trigger_rebuild_on_a_healthy_store_runs_as_before(store, missing):
    path, _before = store
    _drop_triggers(path, missing)
    db = SessionDB(db_path=path)
    try:
        assert db._fts_stale is False and db._fts_enabled is True and db._trigram_available is True
        with _raw(path) as conn:
            present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
            eligible = conn.execute("SELECT COUNT(*) FROM messages WHERE role <> 'tool'").fetchone()[0]
            assert conn.execute("SELECT COUNT(*) FROM messages_fts_trigram_docsize").fetchone()[0] == eligible
        assert set(_FTS_TRIGGERS) <= present
    finally:
        db.close()


def test_a_healthy_open_runs_no_whole_file_check(store, plans):
    path, _before = store
    db = SessionDB(db_path=path)
    try:
        assert db._fts_enabled is True and db._fts_stale is False
    finally:
        db.close()
    fresh = path.parent / "fresh.db"
    SessionDB(db_path=fresh).close()  # a new store: the indexes it creates have nothing to free
    assert plans[1] == []


# ── 5. rebuild_fts() (gateway transcript recovery) ──

def test_rebuild_fts_keeps_a_damaged_index_and_plans_outside_the_lock(store, plans):
    path, before = store
    _break(path, "messages_fts_trigram_data")
    root = _root(path, "messages_fts_trigram_data")
    db = SessionDB(db_path=path)  # the damage is invisible to a normal open
    plans[0]["db"] = db
    assert db._fts_stale is False
    assert db.rebuild_fts() == 0
    assert plans[1] == [False]
    assert db._fts_tick_plan is None  # used for that call only
    _assert_gate_fired(db, path, "messages_fts_trigram_data", root)
    _recover(db, path, before)


def test_rebuild_fts_on_a_healthy_store_runs_as_before(store, plans):
    path, _before = store
    db = SessionDB(db_path=path)
    plans[0]["db"] = db
    try:
        assert db.rebuild_fts() == 2
        assert plans[1] == [False]
        assert db.search_messages("echoword")
    finally:
        db.close()


# ── 6. optimize-storage truncation ('delete-all') ──

def _claim_without_progress(path):
    with _raw(path) as conn:
        conn.execute("INSERT INTO state_meta (key, value) VALUES ('fts_rebuild_high_water', '42') "
                     "ON CONFLICT(key) DO UPDATE SET value = '42'")
        conn.execute("DELETE FROM state_meta WHERE key = 'fts_rebuild_progress'")


def test_truncation_keeps_a_damaged_index(store, plans):
    path, before = store
    _break(path, "messages_fts_trigram_data")
    _claim_without_progress(path)
    root = _root(path, "messages_fts_trigram_data")
    db = SessionDB(db_path=path)
    plans[0]["db"] = db
    assert db.optimize_fts_storage(vacuum=False) == {"ok": False, "reason": "fts_index_damaged"}
    assert plans[1] == [False]
    assert _meta(path, "fts_rebuild_progress") is None  # no claim over rows the index still holds
    _assert_gate_fired(db, path, "messages_fts_trigram_data", root)
    with _raw(path) as conn:
        conn.execute("DELETE FROM state_meta WHERE key = 'fts_rebuild_high_water'")
    _recover(db, path, before)


def test_truncation_on_a_healthy_store_runs_as_before(store, plans):
    path, _before = store
    _claim_without_progress(path)
    db = SessionDB(db_path=path)
    plans[0]["db"] = db
    try:
        db._repair_optimize_bookkeeping()
        assert plans[1] == [False]
        assert _meta(path, "fts_rebuild_progress") == "0"
        with _raw(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages_fts_docsize").fetchone()[0] == 0
    finally:
        db.close()


def test_a_runtime_free_reached_under_the_lock_without_a_plan_skips(store, plans):
    path, _before = store
    db = SessionDB(db_path=path)
    plans[0]["db"] = db
    try:
        with db._lock:
            assert db._reset_fts_index_to_empty(db._conn) is False
            assert db._fts_wholesale_free_allowed((), context="test") is False
        assert plans[1] == []
        assert db._fts_stale is False
        with _raw(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages_fts_docsize").fetchone()[0] > 0
    finally:
        db.close()


# ── 7. trash teardown ──

def _foreign_trash(path):
    """Trash no plan vouched for (optimize-storage's demotion, CJK retirement): the trigram shadow tables renamed
    to ``fts_v22_trash_*``, a fresh empty trigram index in their place."""
    with _raw(path) as conn:
        _drop_triggers(path, _TRIGRAM_TRIGGERS)
        conn.execute("DROP VIEW IF EXISTS messages_fts_trigram_src")
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("DELETE FROM sqlite_master WHERE name = 'messages_fts_trigram' "
                     "AND sql LIKE 'CREATE VIRTUAL TABLE%'")
        conn.execute("PRAGMA writable_schema=RESET")
        conn.execute("PRAGMA legacy_alter_table=ON")
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                            "AND name LIKE 'messages_fts_trigram_%'")]
        for name in names:
            conn.execute(f"ALTER TABLE {name} RENAME TO fts_v22_trash_{name}")
        conn.executescript(FTS_TRIGRAM_SQL)
    return [f"fts_v22_trash_{n}" for n in names]


def _tables(path, like):
    with _raw(path) as conn:
        return sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ?",
                                                 (like,)))


def test_foreign_trash_named_by_the_check_is_never_drained(store, plans):
    path, before = store
    _break(path, "messages_fts_trigram_data")
    trash = _foreign_trash(path)
    db = SessionDB(db_path=path)
    plans[0]["db"] = db
    try:
        assert db._fts_enabled is True
        assert db.advance_fts_backfill(budget_s=2.0) is True  # work remains: the damaged trash stays
        assert db.advance_fts_backfill(budget_s=2.0) is True
        assert plans[1] == [False]  # one check outside the lock; its refusal is remembered
        assert db.optimize_fts_storage(vacuum=False)["reason"] == "teardown_incomplete"
        assert _tables(path, "fts_v22_trash_%") == sorted(trash)
        assert _meta(path, hermes_state_fts_online.TRASH_VERIFIED_KEY) is None
    finally:
        db.close()
    assert _canonical(path)[:len(before)] == before
    assert any("2nd reference" in line for line in _integrity_lines(path)[0])  # left for `hermes sessions recover`


def test_foreign_trash_drains_after_a_clean_plan(store, plans):
    path, _before = store
    _foreign_trash(path)
    db = SessionDB(db_path=path)
    plans[0]["db"] = db
    try:
        _drain(db)
        assert plans[1] == [False]  # one plan vouches for every foreign table; later ticks need none
        assert _tables(path, "fts_v22_trash_%") == []
        assert _meta(path, hermes_state_fts_online.TRASH_VERIFIED_KEY) is None
    finally:
        db.close()
    assert _integrity_lines(path)[0] == ["ok"]


def test_trash_of_an_online_recovery_drains_without_a_new_plan(store, plans):
    path, _before = store
    db = SessionDB(db_path=path)
    plans[0]["db"] = db
    try:
        assert db.recover_stale_fts_online(reason="test", force=True) is True
        trash = _tables(path, "fts_v22_trash_%")
        assert trash and sorted(
            json.loads(_meta(path, hermes_state_fts_online.TRASH_VERIFIED_KEY))) == trash
        assert len(plans[1]) == 1
        _drain(db)
        assert len(plans[1]) == 1  # the recovery's own plan vouched for its trash
        assert _tables(path, "fts_v22_trash_%") == []
        assert _meta(path, hermes_state_fts_online.TRASH_VERIFIED_KEY) is None
    finally:
        db.close()
    assert _integrity_lines(path)[0] == ["ok"]


def test_verified_list_merges_with_trash_already_recorded(tmp_path):
    path = tmp_path / "state.db"
    _seed(path, sessions=1, per=4)
    with _raw(path) as conn:
        conn.execute("INSERT INTO state_meta (key, value) VALUES (?, '[\"fts_v22_trash_older\"]')",
                     (hermes_state_fts_online.TRASH_VERIFIED_KEY,))
    db = SessionDB(db_path=path)
    try:
        assert db.recover_stale_fts_online(reason="test", force=True) is True
    finally:
        db.close()
    with _raw(path) as conn:
        recorded = hermes_state_fts_online.verified_trash(conn)
    assert "fts_v22_trash_older" in recorded and len(recorded) > 1

