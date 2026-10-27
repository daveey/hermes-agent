"""Atomic exact-session acceptance survives lost replies without another user row."""
import hashlib
import json

import pytest

from hermes_state import SessionDB
from hermes_state_prompt_admission import admit_prompt, read_admission


@pytest.fixture(autouse=True)
def no_update_recovery(monkeypatch):
    # A lazy transcript import boots the CLI; recovery belongs to startup, not
    # this scratch-store test, and this worktree's gitdir is in the live home.
    from hermes_cli import _early_recovery
    monkeypatch.setattr(_early_recovery, "restore_interrupted_pull", lambda root: False)
    from pm import environments
    monkeypatch.setattr(environments, "activate_dependencies", lambda root: None)


def digest(sid, text):
    return hashlib.sha256(json.dumps({"stored_session_id": sid, "text": text},
                                    sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def test_atomic_accept_and_reopen_readback(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session(session_id="exact", source="desktop")
    d = digest("exact", "approved scope")
    receipt, staged, created = admit_prompt(db, "exact", "op", "approved scope", d)
    assert created and staged["_row_id"] == receipt["user_row_id"]
    db.close()
    db = SessionDB(db_path=path)
    try:
        assert read_admission(db, "exact", "op", d) == receipt
        again, _, created = admit_prompt(db, "exact", "op", "approved scope", d)
        assert again == receipt and not created
        assert len(db.get_messages("exact")) == 1
        assert db.get_session("exact")["message_count"] == 1
        with pytest.raises(ValueError, match="conflict"):
            admit_prompt(db, "exact", "op", "different", digest("exact", "different"))
        with pytest.raises(ValueError, match="conflict"):
            read_admission(db, "other", "op", d)
    finally:
        db.close()


def test_missing_target_and_bad_digest_write_nothing(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        with pytest.raises(ValueError):
            admit_prompt(db, "missing", "op", "hello", digest("missing", "hello"))
        db.create_session(session_id="exact", source="desktop")
        with pytest.raises(ValueError, match="digest"):
            admit_prompt(db, "exact", "op", "hello", "0" * 64)
        assert db.get_messages("exact") == []
        assert read_admission(db, "exact", "op", digest("exact", "hello")) is None
    finally:
        db.close()


def test_transcript_failure_rolls_back_receipt(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="exact", source="desktop")
    d = digest("exact", "hello")
    def fail(*args, **kwargs):
        raise RuntimeError("injected write failure")
    monkeypatch.setattr(db, "_insert_message_rows", fail)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            admit_prompt(db, "exact", "op", "hello", d)
        assert read_admission(db, "exact", "op", d) is None
        assert db.get_messages("exact") == []
    finally:
        db.close()
