"""Wholesale FTS free gates: fts_free_gate_runtime.

Split by lifecycle path to keep each real-corruption fixture group within the
300-second per-file budget on shared macOS utility-QoS runners.
"""


import sqlite3


import pytest

import hermes_state_fts_online

import hermes_state_schema_memo

from hermes_state import SessionDB


from hermes_state_common import FTS_STALE_KEY

from tests.hermes_state.test_fts_online_recovery import _canonical, _integrity_lines, _meta, _raw, _seed


from tests.hermes_state import fts_free_gate_helpers as fixtures
from tests.hermes_state.fts_free_gate_helpers import (
    _assert_gate_fired,
    _behind_v30,
    _break,
    _recover,
    _root,
    _share_across_families,
    _stale_source_view,
)

def test_optimize_keeps_a_damaged_index_and_plans_outside_the_lock(store, plans):
    path, before = store
    _break(path, "messages_fts_trigram_data")
    root = _root(path, "messages_fts_trigram_data")
    db = SessionDB(db_path=path)
    plans[0]["db"] = db
    with pytest.raises(hermes_state_fts_online.FtsFreeRefused):
        db.optimize_fts()  # and so vacuum() stops before its VACUUM
    assert plans[1] == [False]
    assert db._fts_tick_plan is None
    _assert_gate_fired(db, path, "messages_fts_trigram_data", root)
    _recover(db, path, before)

# Register shared fixtures without importing names shadowed by test arguments.
store = fixtures.store
plans = fixtures.plans


def test_optimize_on_a_healthy_store_runs_as_before(store, plans):
    path, _before = store
    db = SessionDB(db_path=path)
    plans[0]["db"] = db
    try:
        assert db.optimize_fts() == 2
        assert db.vacuum() == 2  # vacuum() merges first, through the same gate
        assert plans[1] == [False, False]
        assert db._fts_stale is False
        assert db.search_messages("bravoword")
    finally:
        db.close()
    assert _integrity_lines(path)[0] == ["ok"]


def test_merge_is_not_gated(store, plans):
    path, _before = store
    db = SessionDB(db_path=path)
    try:
        db._merge_fts_incrementally(max_pages=8, max_commands=2)
        assert plans[1] == []
    finally:
        db.close()


def test_a_subset_free_refuses_when_the_check_names_another_family(tmp_path):
    """integrity_check names a shared page against only one owner. A path that frees only the OTHER family's trees
    must still detach (the reviewer reproduced `messages` reusing the freed page through the trigram migration)."""
    path = tmp_path / "state.db"
    _seed(path, sessions=6, per=300)
    hermes_state_schema_memo.clear()
    before = _canonical(path)
    _named, other = _share_across_families(path)
    if other == "messages_fts_trigram":
        _behind_v30(path)  # the migration rebuilds (frees) the trigram index only
    else:
        _stale_source_view(path, claim=False)  # the realign frees the base index only
    db = SessionDB(db_path=path)
    try:
        assert db._fts_stale is True and db._fts_oneshot_failed is True
        assert _meta(path, FTS_STALE_KEY) == "1"
        for i in range(200):  # writes go on; nothing may reuse the shared page
            db.append_message("s0", "user", f"after {i} " + "w" * 300)
    finally:
        db.close()
    import re

    lines, master = _integrity_lines(path)
    names = {int(rp): name for rp, _t, name in master}
    trees = {names.get(int(m.group(1))) for line in lines if (m := re.match(r"Tree (\d+) ", line))}
    assert not [t for t in trees if hermes_state_fts_online.family_of(t or "") is None], trees
    assert _canonical(path)[:len(before)] == before


def test_a_refusal_is_remembered_across_processes(store, plans, monkeypatch):
    """A refused plan is recorded next to state.db, so the next handle (another process) does not repeat the
    whole-file check; a completed plan forgets it."""
    path, _before = store
    db = SessionDB(db_path=path)
    try:
        monkeypatch.setattr(hermes_state_fts_online, "plan_from_check",
                            lambda lines, master, **_kw: (_ for _ in ()).throw(
                                hermes_state_fts_online.OnlineRecoveryRefused("test damage")))
        with pytest.raises(hermes_state_fts_online.FtsFreeRefused):
            db.optimize_fts()
        assert len(plans[1]) == 1
        assert hermes_state_fts_online.read_refusal_memo(path) == "test damage"
    finally:
        db.close()
    other = SessionDB(db_path=path)
    try:
        with pytest.raises(hermes_state_fts_online.FtsFreeRefused):
            other.optimize_fts()
        assert len(plans[1]) == 1  # remembered: no second check
        monkeypatch.undo()
        hermes_state_fts_online.write_refusal_memo(path, None)
        assert other.optimize_fts() == 2
        assert hermes_state_fts_online.read_refusal_memo(path) is None
    finally:
        other.close()


def test_vacuum_stops_when_the_gate_keeps_the_index(store, monkeypatch):
    path, _before = store
    _break(path, "messages_fts_trigram_data")
    db = SessionDB(db_path=path)
    try:
        with pytest.raises(hermes_state_fts_online.FtsFreeRefused):
            db.vacuum()
        assert db._fts_stale is True
        assert any("2nd reference" in line for line in _integrity_lines(path)[0])  # not rewritten
    finally:
        db.close()


def test_the_attempt_plan_is_per_thread(store):
    """A gateway SessionDB is shared by threads: one thread's plan is never used or cleared by another."""
    import threading

    path, _before = store
    db = SessionDB(db_path=path)
    try:
        db._fts_tick_plan = ("ok", {"unlink": [], "trash": [], "roots": {}, "problems": 0})
        seen = []
        t = threading.Thread(target=lambda: (seen.append(db._fts_tick_plan), setattr(db, "_fts_tick_plan", None)))
        t.start()
        t.join()
        assert seen == [None]
        assert db._fts_tick_plan is not None
    finally:
        db._fts_tick_plan = None
        db.close()


def test_detach_waits_out_a_short_write_lock_and_commits_atomically(store):
    import threading

    path, _before = store
    db = SessionDB(db_path=path)
    try:
        holder = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        holder.execute("BEGIN IMMEDIATE")
        threading.Timer(0.6, holder.commit).start()
        with db._lock:
            db._detach_fts_stale(db._conn.cursor())
        assert db._fts_stale is True
        assert _meta(path, FTS_STALE_KEY) == "1"
        with _raw(path) as conn:
            assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name LIKE "
                                    "'messages_fts%'").fetchone()
        holder.close()
    finally:
        db.close()


