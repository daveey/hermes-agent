"""A read-only browse open probes the declared schema once per store schema, not once per open. [daveey]

``/api/profiles/sessions`` opens a fresh read-only ``SessionDB`` for every profile whose ``state.db`` moved, and each
open ran the ~25-statement declared-schema probe (#140315): most of ``_read_profile_db``'s statements, each a GIL
release in the Desktop backend. The probe's answer is a function of the store's schema, which SQLite's
``schema_version`` cookie (bumped by every DDL, from any process) and the file identity pin, so an unchanged store
is not re-probed. These tests pin that drift, a replaced file and a new build's probe are still probed.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing

import pytest

from hermes_cli import web_server_sessions as wss
from hermes_state import SessionDB


class _Probes(tuple):
    """The probe statements, counting how often an open runs them."""

    runs = 0

    def __iter__(self):
        type(self).runs += 1
        return super().__iter__()


@pytest.fixture
def probes(monkeypatch):
    real = wss._session_db_read_probe_statements()
    _Probes.runs = 0
    counted = _Probes(real)
    monkeypatch.setattr(wss, "_session_db_read_probe_statements", lambda: counted)
    getattr(wss, "_PROBED_STORES", set()).clear()
    yield _Probes
    getattr(wss, "_PROBED_STORES", set()).clear()


def _store(path):
    """A settled store in an unowned profile home (the shape ``test_browse_schema_drift`` browses)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / "config.yaml").write_text("{}\n", encoding="utf-8")
    with SessionDB(db_path=path) as db:
        db.create_session("kept", source="cli")
    return path


def _browse(path):
    db = wss._open_session_db_at_path(path, read_only=True)
    try:
        return [s["id"] for s in db.list_sessions_rich(limit=5)]
    finally:
        db.close()


def test_an_unchanged_store_is_probed_once(tmp_path, probes):
    path = _store(tmp_path / "profiles" / "scribe" / "state.db")
    for _ in range(3):
        assert _browse(path) == ["kept"]
    assert probes.runs == 1


def test_a_write_that_is_not_ddl_keeps_the_probe(tmp_path, probes):
    path = _store(tmp_path / "profiles" / "scribe" / "state.db")
    _browse(path)
    with SessionDB(db_path=path) as db:
        db.create_session("more", source="cli")
    assert sorted(_browse(path)) == ["kept", "more"]
    assert probes.runs == 1


def test_ddl_after_a_passed_probe_is_probed_and_drift_is_still_caught(tmp_path, probes):
    path = _store(tmp_path / "profiles" / "scribe" / "state.db")
    _browse(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("DROP TRIGGER IF EXISTS sessions_runtime_ledger_delete_guard")
        conn.execute("DROP TRIGGER IF EXISTS session_change_on_sessions_update")
        conn.execute("ALTER TABLE sessions DROP COLUMN runtime_generation")
        conn.commit()
    assert _browse(path) == ["kept"]  # the probe found the drift and the unowned store was caught up (#140315)
    with closing(sqlite3.connect(path)) as conn:
        assert "runtime_generation" in {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert probes.runs >= 2


def test_a_replaced_store_is_probed_again(tmp_path, probes):
    path = _store(tmp_path / "profiles" / "scribe" / "state.db")
    _browse(path)
    other = _store(tmp_path / "profiles" / "other" / "state.db")
    os.replace(other, path)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(f"{path}{suffix}"):
            os.remove(f"{path}{suffix}")
    assert _browse(path) == ["kept"]
    assert probes.runs == 2


def test_a_different_probe_set_is_run(tmp_path, probes, monkeypatch):
    path = _store(tmp_path / "profiles" / "scribe" / "state.db")
    _browse(path)
    changed = _Probes(tuple.__add__(wss._session_db_read_probe_statements(), ("SELECT 1",)))  # not counted
    monkeypatch.setattr(wss, "_session_db_read_probe_statements", lambda: changed)
    _browse(path)
    assert probes.runs == 2


def test_a_failed_probe_is_not_remembered(tmp_path, probes, monkeypatch):
    from fastapi import HTTPException

    path = _store(tmp_path / "profiles" / "scribe" / "state.db")
    broken = _Probes(("SELECT missing_column FROM sessions LIMIT 0",))
    monkeypatch.setattr(wss, "_session_db_read_probe_statements", lambda: broken)
    monkeypatch.setattr("hermes_state_reconcile.is_declarative_drift_error", lambda exc: False)
    for _ in range(2):
        with pytest.raises(HTTPException):
            _browse(path)
    assert probes.runs == 2 and not getattr(wss, "_PROBED_STORES", set())
