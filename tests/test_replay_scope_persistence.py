"""Legacy replay uncertainty must survive native durable history rewrites."""
import pytest
from hermes_state import SessionDB


@pytest.mark.parametrize("metadata", [None, {"label": "keep"}])
def test_sanitized_scope_survives_repeated_compaction(tmp_path, metadata):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("legacy", source="cli")
        db.append_message("legacy", "user", "<memory-context>Do not publish.</memory-context>",
                          display_metadata=metadata)
        for _ in range(3):
            rows = db.get_messages_as_conversation("legacy")
            assert rows[0]["content"] == ""
            assert rows[0]["_user_content_sanitized"] is True
            db.archive_and_compact("legacy", rows, tail_count=len(rows))
        rows = db.get_messages_as_conversation("legacy")
        assert rows[0]["_user_content_sanitized"] is True
        assert rows[0]["display_metadata"]["user_content_sanitized"] is True
        if metadata:
            assert rows[0]["display_metadata"]["label"] == "keep"
    finally:
        db.close()
