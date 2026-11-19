"""Futile-deferral detection under a churning holder set.

Regression for the hub's 2026-11-18/19 stale index: foreign holders were present at every one of 2,213 deferrals
over two days, but the observed PID set changed every few deferrals (short-lived CLI holders, and each process
records its own view, which leaves itself out). The futility window keyed on an identical set, so it never closed
and the automatic online recovery never ran. Futility is continuous presence of any foreign holder.
"""

import json
import sqlite3

import pytest

import hermes_state_schema
from hermes_state import SessionDB
from hermes_state_common import FTS_REBUILD_DEFERRAL_KEY


def _meta_value(db_path, key):
    raw = sqlite3.connect(str(db_path))
    try:
        row = raw.execute(
            "SELECT value FROM state_meta WHERE key = ?", (key,)
        ).fetchone()
    finally:
        raw.close()
    return None if row is None else row[0]


@pytest.fixture
def stale_store(tmp_path, monkeypatch):
    """A stale store reopened while foreign holders change on every scan; yields (db, db_path, clock)."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    if not db._fts_enabled:
        db.close()
        pytest.skip("FTS5 unavailable in this build")
    db.create_session("s1", source="test")
    db.append_message("s1", "user", "seed")
    raw = sqlite3.connect(str(db_path))
    raw.execute(
        "UPDATE messages_fts_data SET block = X'DEADBEEFDEADBEEFDEADBEEFDEADBEEF'"
    )
    raw.commit()
    raw.close()
    monkeypatch.setattr(
        db,
        "rebuild_fts",
        lambda: (_ for _ in ()).throw(sqlite3.DatabaseError("still corrupt")),
    )
    db.append_message("s1", "user", "before restart")
    db.close()

    # Two processes each seeing the other (never itself), plus a transient CLI holder now and then.
    scans = [0]

    def holders(self):
        scans[0] += 1
        pid = 4242 if scans[0] % 2 else 5151
        found = [(pid, str(db_path) + "-wal")]
        if scans[0] % 3 == 0:
            found.append((9000 + scans[0], str(db_path)))
        return found

    monkeypatch.setattr(SessionDB, "_foreign_state_db_holders", holders)
    monkeypatch.setattr(
        SessionDB,
        "_reap_inactive_orphan_desktop_holders",
        lambda self, h, *, min_age_seconds: [],
    )
    clock = [1000.0]
    monkeypatch.setattr(hermes_state_schema.time, "time", lambda: clock[0])
    reopened = SessionDB(db_path=db_path)
    try:
        assert reopened._fts_stale is True
        yield reopened, db_path, clock
    finally:
        reopened.close()


def _defer(db, clock, step):
    clock[0] += step
    cursor = db._conn.cursor()
    assert db._recover_stale_fts(cursor, legacy=False, timeout_seconds=0.0) is False
    db._conn.commit()
    return json.loads(_meta_value(db.db_path, FTS_REBUILD_DEFERRAL_KEY))


def test_holders_present_at_every_deferral_turn_futile_even_when_the_pids_change(
    stale_store, caplog
):
    db, _db_path, clock = stale_store
    attempts = hermes_state_schema._FTS_HOLDER_FUTILE_ATTEMPTS
    step = hermes_state_schema._FTS_HOLDER_FUTILE_SECONDS / (attempts - 1)
    seen = set()
    record = {}
    for _ in range(
        attempts
    ):  # the open deferred once already, so the window ends inside this loop
        caplog.clear()
        record = _defer(db, clock, step)
        seen.add(tuple(record["holder_pids"]))
        if record.get("futile"):
            break
    assert len(seen) > 1, (
        "the holder set must actually churn for this test to mean anything"
    )
    assert record.get("futile") is True and record["holders_attempts"] == attempts, (
        record
    )
    assert SessionDB._fts_deferral_futile(db._conn.cursor()) is True
    futile_lines = [
        r.getMessage() for r in caplog.records if "waiting is futile" in r.getMessage()
    ]
    assert futile_lines and "Stop ONLY the other holder" in futile_lines[-1]


def test_a_quiet_gap_longer_than_the_retry_cap_starts_a_new_window(stale_store):
    db, _db_path, clock = stale_store
    attempts = hermes_state_schema._FTS_HOLDER_FUTILE_ATTEMPTS
    step = hermes_state_schema._FTS_HOLDER_FUTILE_SECONDS / (attempts - 1)
    for _ in range(attempts - 2):  # plus the open's deferral: one short of the window
        record = _defer(db, clock, step)
    assert record["holders_attempts"] == attempts - 1 and not record.get("futile"), (
        record
    )
    # No deferral recorded for longer than any retry backoff: an old episode, not continuous presence.
    record = _defer(db, clock, hermes_state_schema._FTS_STALE_RETRY_MAX_SECONDS * 2 + 1)
    assert record["holders_attempts"] == 1 and not record.get("futile")
