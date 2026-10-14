"""``minted_session_keys``: the store-local record ``session.resume`` checks before materializing a row."""

from __future__ import annotations

import time

from hermes_state import SessionDB


def test_record_and_lookup_is_idempotent(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        assert not db.was_session_key_minted("20261013_184022_b1a59f")
        db.record_minted_session_key("20261013_184022_b1a59f")
        db.record_minted_session_key("20261013_184022_b1a59f")
        assert db.was_session_key_minted("20261013_184022_b1a59f")
        assert not db.was_session_key_minted("")
        assert db.get_session("20261013_184022_b1a59f") is None, "recording a key must not create a sessions row"
    finally:
        db.close()


def test_record_prunes_records_older_than_ttl(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        stale = time.time() - db.MINTED_SESSION_KEY_TTL_S - 60
        db._write_sql("INSERT INTO minted_session_keys (id, minted_at) VALUES (?, ?)", ("20260801_000000_aaaaaa", stale))
        db.record_minted_session_key("20261013_184022_b1a59f")
        assert not db.was_session_key_minted("20260801_000000_aaaaaa")
        assert db.was_session_key_minted("20261013_184022_b1a59f")
    finally:
        db.close()
