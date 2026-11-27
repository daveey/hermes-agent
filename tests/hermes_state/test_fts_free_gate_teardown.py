"""Wholesale FTS free gates: fts_free_gate_teardown.

Split by lifecycle path to keep each real-corruption fixture group within the
300-second per-file budget on shared macOS utility-QoS runners.
"""

import json




import hermes_state_fts_online


from hermes_state import SessionDB



from tests.hermes_state.test_fts_online_recovery import _canonical, _drain, _integrity_lines, _meta, _raw, _seed


from tests.hermes_state import fts_free_gate_helpers as fixtures
from tests.hermes_state.fts_free_gate_helpers import (
    _assert_gate_fired,
    _break,
    _claim_without_progress,
    _foreign_trash,
    _recover,
    _root,
    _tables,
)

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

# Register shared fixtures without importing names shadowed by test arguments.
store = fixtures.store
plans = fixtures.plans


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


def test_truncation_keeps_a_damaged_index(store, plans):
    path, before = store
    _break(path, "messages_fts_trigram_data")
    _claim_without_progress(path)
    root = _root(path, "messages_fts_trigram_data")
    db = SessionDB(db_path=path)
    plans[0]["db"] = db
    assert db.optimize_fts_storage(vacuum=False) == {"ok": False, "reason": "fts_index_kept"}
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


def test_foreign_trash_named_by_the_check_is_never_drained(store, plans):
    path, before = store
    _break(path, "messages_fts_trigram_data")
    trash = _foreign_trash(path)
    db = SessionDB(db_path=path)
    plans[0]["db"] = db
    try:
        assert db._fts_enabled is True
        # The damaged trash stays, and no tick can do anything about it while the refusal is remembered: no work.
        assert db.advance_fts_backfill(budget_s=2.0) is False
        assert db.advance_fts_backfill(budget_s=2.0) is False
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
        conn.execute("CREATE TABLE fts_v22_trash_older (id INTEGER PRIMARY KEY, block BLOB)")
        conn.execute("CREATE TABLE fts_v22_trash_legacy (id INTEGER PRIMARY KEY, block BLOB)")
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'fts_v22_trash_older'").fetchone()[0]
        conn.execute("INSERT INTO state_meta (key, value) VALUES (?, ?)",
                     (hermes_state_fts_online.TRASH_VERIFIED_KEY, json.dumps({"fts_v22_trash_older": root})))
    db = SessionDB(db_path=path)
    try:
        assert db.recover_stale_fts_online(reason="test", force=True) is True
    finally:
        db.close()
    with _raw(path) as conn:
        recorded = hermes_state_fts_online.verified_trash(conn)
    assert "fts_v22_trash_older" in recorded and len(recorded) > 1
    assert "fts_v22_trash_legacy" not in recorded


def test_verified_trash_ignores_the_old_list_format_and_a_reused_name(tmp_path):
    """A list entry carries no root page, and a table re-created under a recorded name is a different tree: neither
    is drained without a new plan."""
    path = tmp_path / "state.db"
    _seed(path, sessions=1, per=4)
    key = hermes_state_fts_online.TRASH_VERIFIED_KEY
    with _raw(path) as conn:
        conn.execute("CREATE TABLE fts_v22_trash_a (id INTEGER PRIMARY KEY, block BLOB)")
        conn.execute("INSERT INTO state_meta (key, value) VALUES (?, '[\"fts_v22_trash_a\"]')", (key,))
        assert hermes_state_fts_online.verified_trash(conn) == set()
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'fts_v22_trash_a'").fetchone()[0]
        conn.execute("UPDATE state_meta SET value = ? WHERE key = ?", (json.dumps({"fts_v22_trash_a": root}), key))
        assert hermes_state_fts_online.verified_trash(conn) == {"fts_v22_trash_a"}
        conn.execute("DROP TABLE fts_v22_trash_a")
        conn.execute("CREATE TABLE filler (x)")  # takes the freed root page
        conn.execute("CREATE TABLE fts_v22_trash_a (id INTEGER PRIMARY KEY, block BLOB)")
        new_root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'fts_v22_trash_a'").fetchone()[0]
        assert new_root != root
        assert hermes_state_fts_online.verified_trash(conn) == set()


