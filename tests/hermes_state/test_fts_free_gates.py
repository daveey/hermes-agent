"""Wholesale FTS free gates: fts_free_gates.

Split by lifecycle path to keep each real-corruption fixture group within the
300-second per-file budget on shared macOS utility-QoS runners.
"""




import pytest

import hermes_state_fts_online


from hermes_state import SessionDB

from hermes_state_fts import base_source_view_is_stale

from hermes_state_common import FTS_STALE_KEY, SCHEMA_VERSION, _FTS_TRIGGERS

from tests.hermes_state.test_fts_online_recovery import _meta, _raw


from tests.hermes_state import fts_free_gate_helpers as fixtures
from tests.hermes_state.fts_free_gate_helpers import (
    _TRIGRAM_TRIGGERS,
    _assert_gate_fired,
    _behind_v30,
    _break,
    _drop_triggers,
    _orphan,
    _recover,
    _root,
    _schema_version,
    _stale_source_view,
)

def test_realign_keeps_a_damaged_base_index(store, plans):
    path, before = store
    _break(path, "messages_fts_data")
    _stale_source_view(path, claim=False)
    root = _root(path, "messages_fts_data")
    db = SessionDB(db_path=path)
    _assert_gate_fired(db, path, "messages_fts_data", root)
    assert plans[1] == [None]  # one whole-file check for the whole open
    _recover(db, path, before)

# Register shared fixtures without importing names shadowed by test arguments.
store = fixtures.store
plans = fixtures.plans


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


