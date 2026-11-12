"""Addressed user-row content rewrites for SessionDB."""
from typing import Any, Optional
from hermes_state_common import _MERGE_DISPLAY_META, QUEUED_PROMPT_METADATA_KEY, _sql_json_extract


def adopt_queued_user_row_in_place(db, session_id: str, row_id: Any) -> int:
    """Turn a still-marked queued accept row into the dispatching turn's own row, IN PLACE (one row per submit).

    Only when it is still the session's LAST active row: nothing landed after it, so the turn's rows follow it
    and alternation holds without re-appending a copy. Drops the never-drained marker (``reopen_session`` would
    otherwise retire the row after a restart) and keeps every other display key. One UPDATE, so a row another
    writer appends concurrently either lands first (no match: the caller re-places as before) or after.
    Returns the affected row count (0 = not adoptable here)."""
    if not session_id or isinstance(row_id, bool) or not isinstance(row_id, int) or row_id <= 0:
        return 0
    marker = _sql_json_extract("display_metadata", "$." + QUEUED_PROMPT_METADATA_KEY)
    return db._write_rowcount(
        "UPDATE messages SET display_metadata = NULLIF(json_remove(display_metadata, ?), '{}') "
        "WHERE id = ? AND session_id = ? AND role = 'user' AND active = 1 "
        f"AND COALESCE({marker}, 0) = 1 "
        "AND NOT EXISTS (SELECT 1 FROM messages later WHERE later.session_id = ? AND later.active = 1 "
        "AND later.id > ?)",
        ("$." + QUEUED_PROMPT_METADATA_KEY, row_id, session_id, session_id, row_id))


def set_user_message_content(db, session_id: str, row_id: int, content: Any,
                             display_metadata: Optional[dict[str, Any]] = None) -> int:
    """Rewrite one active user row; a veto's display copy clears its API sidecar.

    Submit-time rows must replay the expanded prompt or withheld placeholder rather
    than raw keystrokes, without appending another row for the same input.
    Display metadata is merge-patched: None deletes keys and reactions survive.
    """
    if not session_id or isinstance(row_id, bool) or not isinstance(row_id, int) or row_id <= 0:
        return 0
    if display_metadata is None:
        return db._write_rowcount(
            "UPDATE messages SET content = ? WHERE id = ? AND session_id = ? AND role = 'user' AND active = 1",
            (db._encode_content(content), row_id, session_id))
    return db._write_rowcount(
        f"UPDATE messages SET content = ?, api_content = NULL, display_metadata = {_MERGE_DISPLAY_META} "
        "WHERE id = ? AND session_id = ? AND role = 'user' AND active = 1",
        (db._encode_content(content), db._encode_display_metadata(display_metadata), row_id, session_id))
