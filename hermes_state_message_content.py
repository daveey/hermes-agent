"""Addressed user-row content rewrites for SessionDB."""
from typing import Any, Optional
from hermes_state_common import _MERGE_DISPLAY_META


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
