"""Follow settled notice tails without hiding the preceding obligation. [daveey]"""
from __future__ import annotations

from typing import Any


def preceding_candidate(db: Any, row: Any) -> dict:
    """Only superseded notice rows are transparent, never replies or failed wakes.

    Bound the exceptional lookup to the same window as outcome derivation. Normal
    discovery keeps its indexed scan and does no extra reads.
    """
    from hermes_state_outcome import _TAIL_ROWS
    from tui_gateway.notice_fanout import _decode

    row = dict(row)
    closeout = _decode(row["display_metadata"]).get("closeout")
    if row["role"] != "user" or not isinstance(closeout, dict) or closeout.get("state") != "superseded":
        return row
    previous = db._read_all(
        "SELECT id AS msg_id, role, timestamp, content, display_kind, display_metadata FROM messages "
        "WHERE session_id = ? AND active = 1 AND role IN ('user', 'assistant', 'tool') "
        "AND id < ? ORDER BY id DESC LIMIT ?", (row["sid"], row["msg_id"], _TAIL_ROWS - 1))
    for earlier in previous:
        if earlier["role"] != "user" or earlier["display_kind"] != "kanban_notice":
            return row
        closeout = _decode(earlier["display_metadata"]).get("closeout")
        if not isinstance(closeout, dict) or closeout.get("state") != "superseded":
            return {**row, **dict(earlier)}
    return row


def creator_superseded(cand: dict) -> bool:
    """Retire only an exact local producer row whose creator question is no longer pending.

    Revalidate the event and rendered content: a folded row may carry unrelated
    input while retaining the original notice metadata. Missing provenance fails
    closed, as does a current chat park (scheduled alone is not proof of settlement).
    """
    import contextlib

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_decision_ask import notice_text
    from tui_gateway import server

    if notice_text({}).split("\n", 1)[0] not in cand["content"]:
        return False
    meta = cand["meta"]
    events = meta.get("event_ids")
    tid, board = meta.get("task_id"), meta.get("board")
    if not isinstance(events, list) or len(events) != 1 or type(events[0]) is not int or not tid or not board:
        return False
    try:
        conn = kbc.connect(board=board)
    except ValueError:
        return False
    with contextlib.closing(conn):
        row = conn.execute("SELECT * FROM task_events WHERE id = ? AND task_id = ? AND kind = ?",
                           (events[0], tid, "decision_requested")).fetchone()
        task = kb.get_task(conn, tid)
        if row is None or task is None:
            return False
        event = kb.Event.from_row(row)
        rendered = server._format_kanban_event_text({"task_id": tid, "chat_id": cand["sid"]}, task, event, board)
        if cand["content"] != rendered:
            return False
        if task.status != "scheduled":
            return True
        park = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind IN "
                            "('scheduled', 'continued') ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        if park is None:
            return False
        import json
        try:
            payload = json.loads(park[0] or "{}")
        except (ValueError, TypeError):
            return False
        awaiting = payload.get("awaiting") if isinstance(payload, dict) else None
        return isinstance(awaiting, dict) and awaiting.get("kind") in ("ci", "deploy", "card", "external", "clock", "resource")

