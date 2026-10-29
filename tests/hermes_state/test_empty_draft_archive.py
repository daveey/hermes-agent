"""Drafts (session-outcome contract, lane A2): maintenance archives interactive rows that never got a message and
are older than 24 h; pinned rows and young drafts stay. The pass runs from the gateway's state.db maintenance
tick whatever ``sessions.auto_archive`` says. [daveey]"""

from __future__ import annotations

import time

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    handle = SessionDB(db_path=tmp_path / "state.db")
    yield handle
    handle.close()


def _draft(db, sid, age_h, *, source="desktop", pinned=False):
    db.create_session(sid, source=source)
    then = time.time() - age_h * 3600
    db._conn.execute("UPDATE sessions SET started_at = ?, last_activity_at = ?, pinned = ? WHERE id = ?",
                     (then, then, int(pinned), sid))
    db._conn.commit()


def _archived(db, sid):
    return bool(db.get_session(sid)["archived"])


def test_old_empty_drafts_are_archived_pinned_and_young_ones_stay(db):
    _draft(db, "old", 30)
    _draft(db, "pinned", 30, pinned=True)
    _draft(db, "young", 2)
    _draft(db, "used", 30)
    db.append_message("used", role="user", content="hello")
    db._conn.execute("UPDATE sessions SET last_activity_at = ? WHERE id = 'used'", (time.time() - 30 * 3600,))
    db._conn.execute("UPDATE messages SET timestamp = ? WHERE session_id = 'used'", (time.time() - 30 * 3600,))
    db._conn.commit()
    _draft(db, "telegram", 30, source="telegram")

    assert db.maybe_archive_empty_drafts() == 1

    assert [s for s in ("old", "pinned", "young", "used", "telegram") if _archived(db, s)] == ["old"]
    assert db.maybe_archive_empty_drafts() == 0  # throttled for the hour


def test_gateway_maintenance_tick_archives_drafts_without_auto_archive(monkeypatch, db):
    from gateway import run
    from hermes_cli import config
    import hermes_state_registry
    _draft(db, "old", 30)
    monkeypatch.setattr(config, "load_config", lambda: {"sessions": {"auto_archive": False, "auto_prune": False}})
    monkeypatch.setattr(hermes_state_registry, "acquire", lambda *a, **k: db)
    monkeypatch.setattr(hermes_state_registry, "release_or_close", lambda handle: None)

    run._housekeeping_state_db_maintenance()

    assert _archived(db, "old")


def test_empty_sweep_writes_nothing_and_ids_are_listed_read_only(db):
    """A sweep that finds no draft stamps no state_meta row (GET /api/sessions polls reach it), and
    ``empty_draft_ids`` lets a read-only handle ask before the store is opened writable."""
    _draft(db, "young", 2)
    assert db.empty_draft_ids() == []
    assert db.maybe_archive_empty_drafts() == 0
    assert db.get_meta("last_draft_archive") is None
    _draft(db, "old", 30)
    assert db.empty_draft_ids() == ["old"]
