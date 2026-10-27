"""Admission preserves reopen and durable-message identity contracts."""
import json
import pytest

from hermes_state import SessionDB
from hermes_state_prompt_admission import admit_prompt, read_admission, prompt_digest
from tests.hermes_state.test_prompt_admission import no_update_recovery


def test_reopen_retires_queue_unarchives_and_freezes_reset_child(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("exact", "desktop", session_key="lane")
        db.append_message("exact", "user", "never ran", display_metadata={"_queued_prompt": True})
        db.end_session("exact", "session_reset")
        db.create_session("child", "desktop", session_key="lane", parent_session_id="exact")
        db._write_sql("UPDATE sessions SET archived=1, auto_archived=1 WHERE id=?", ("exact",))
        admit_prompt(db, "exact", "op", "hello", prompt_digest("exact", "hello"))
        assert [m["content"] for m in db.get_messages("exact")] == ["hello"]
        row = db.get_session("exact")
        assert row["ended_at"] is None
        assert row["archived"] == row["auto_archived"] == 0
        assert json.loads(db.get_session("child")["model_config"])["_reset_from"] == "exact"
    finally:
        db.close()


def test_readback_survives_reinserted_inactive_uid(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("exact", "desktop")
        d = prompt_digest("exact", "hello")
        receipt, _, _ = admit_prompt(db, "exact", "op", "hello", d)
        messages = db.get_messages_as_conversation("exact")
        messages[0]["content"] = "expanded hello"
        db.replace_messages("exact", messages)
        rewritten = db.get_messages_as_conversation("exact", include_row_ids=True)
        assert rewritten[0]["_row_id"] != receipt["user_row_id"]
        assert read_admission(db, "exact", "op", d) == receipt
        db._write_sql("UPDATE messages SET active=0 WHERE session_id=?", ("exact",))
        assert read_admission(db, "exact", "op", d) == receipt
    finally:
        db.close()


def test_failed_insert_rolls_back_reopen_invariants(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("exact", "desktop")
        db.append_message("exact", "user", "queued", display_metadata={"_queued_prompt": True})
        db.end_session("exact", "cli_close")
        db._write_sql("UPDATE sessions SET archived=1, auto_archived=1 WHERE id=?", ("exact",))
        def fail(*args):
            raise RuntimeError("insert failed")
        monkeypatch.setattr(db, "_insert_message_rows", fail)
        with pytest.raises(RuntimeError, match="insert failed"):
            admit_prompt(db, "exact", "op", "hello", prompt_digest("exact", "hello"))
        assert db.get_session("exact")["ended_at"] is not None
        assert db.get_session("exact")["auto_archived"] == 1
        assert [m["content"] for m in db.get_messages("exact")] == ["queued"]
        assert read_admission(db, "exact", "op", prompt_digest("exact", "hello")) is None
    finally:
        db.close()


def test_live_session_does_not_retire_current_queue(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("exact", "desktop")
        db.append_message("exact", "user", "queued", display_metadata={"_queued_prompt": True})
        admit_prompt(db, "exact", "op", "hello", prompt_digest("exact", "hello"))
        assert [m["content"] for m in db.get_messages("exact")] == ["queued", "hello"]
    finally:
        db.close()


def test_legacy_receipt_schema_read_and_migration(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("exact", "desktop")
        d = prompt_digest("exact", "hello")
        receipt, _, _ = admit_prompt(db, "exact", "op", "hello", d)
        db._write_sql("ALTER TABLE prompt_admissions DROP COLUMN dispatch", ())
        assert read_admission(db, "exact", "op", d) == receipt
        assert admit_prompt(db, "exact", "op", "hello", d) == (receipt, None, False)
        db._write_sql("UPDATE messages SET message_uid='replacement' WHERE session_id=?", ("exact",))
        with pytest.raises(ValueError, match="no longer verifiable"):
            read_admission(db, "exact", "op", d)
    finally:
        db.close()
