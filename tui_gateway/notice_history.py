"""Follow proven settled notice tails without hiding other input. [daveey]"""
from __future__ import annotations

import hashlib
from typing import Any


def content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def settled_notice(row: dict) -> bool:
    """Only a whole-row receipt admits transparency; legacy stamps are not proof.

    A blocked event can share a batch (or replay) with unrelated input. Neither
    stale flags nor a historical whole-row superseded stamp attest that input.
    Bind the admission to content so later alternation folding invalidates it.
    """
    from tui_gateway.notice_fanout import _decode

    meta = _decode(row.get("display_metadata"))
    closeout = meta.get("closeout")
    content = row.get("content")
    return (row.get("role") == "user" and row.get("display_kind") == "kanban_notice"
            and isinstance(content, str) and isinstance(closeout, dict)
            and closeout.get("state") == "superseded"
            and closeout.get("settled_content_sha256") == content_digest(content))


def preceding_candidate(db: Any, row: Any) -> dict:
    """Traverse proven settlement only, within outcome derivation's tail bound."""
    from hermes_state_outcome import _TAIL_ROWS

    row = dict(row)
    if not settled_notice(row):
        return row
    previous = db._read_all(
        "SELECT id AS msg_id, role, timestamp, content, display_kind, display_metadata FROM messages "
        "WHERE session_id = ? AND active = 1 AND role IN ('user', 'assistant', 'tool') "
        "AND id < ? ORDER BY id DESC LIMIT ?", (row["sid"], row["msg_id"], _TAIL_ROWS - 1))
    for earlier in previous:
        if earlier["role"] != "user" or earlier["display_kind"] != "kanban_notice":
            return row
        if not settled_notice(dict(earlier)):
            return {**row, **dict(earlier)}
    return row


def creator_superseded(cand: dict) -> bool:
    """Exact local creator rendering plus proof the card left its chat wait."""
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
        with contextlib.closing(kbc.connect(board=board)) as conn:
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
            park = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND id > ? AND kind IN "
                                "('scheduled', 'continued') ORDER BY id DESC LIMIT 1", (tid, events[0])).fetchone()
            if park is None:
                return False
            import json
            try:
                payload = json.loads(park[0] or "{}")
            except (ValueError, TypeError):
                return False
            awaiting = payload.get("awaiting") if isinstance(payload, dict) else None
            return isinstance(awaiting, dict) and awaiting.get("kind") in ("ci", "deploy", "card", "external", "clock", "resource")
    except Exception:  # health: allow BLE001 -- unavailable/malformed source is never settlement evidence
        # No settlement proof on a transient source failure; normal wake custody remains.
        return False


def whole_row_settled(cand: dict) -> bool:
    """Revalidate every local source event, then require an exact complete rendering.

    No regex or stale flag proves purity. Replay wrappers and folded text cannot
    equal the source rendering. Foreign and old provenance-free rows fail closed.
    """
    import contextlib
    from copy import copy

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tui_gateway import server
    from tui_gateway.notice_guarantee import _is_remote

    meta = cand["meta"]
    if _is_remote(meta):
        return False
    if creator_superseded(cand):
        return True
    keys = meta.get("kanban_event_keys")
    if not isinstance(keys, list) or not keys or len(keys) > 50 or not all(isinstance(k, str) for k in keys):
        return False
    if len(set(keys)) != len(keys):
        return False
    rendered = []
    try:
        for key in keys:
            board, sep, eid = key.rpartition(":")
            if not sep or not board or not eid.isdecimal():
                return False
            with contextlib.closing(kbc.connect(board=board)) as conn:
                row = conn.execute("SELECT * FROM task_events WHERE id = ?", (int(eid),)).fetchone()
                if row is None:
                    return False
                event = kb.Event.from_row(row)
                task = kb.get_task(conn, event.task_id)
                if event.kind != "blocked" or task is None or task.status in ("blocked", "triage"):
                    return False
                # Rebuild only fields the block producer reads from this event's
                # snapshot. Changed title/assignee or old missing request data can
                # reject a valid old block; they must never admit unrelated text.
                task = copy(task)
                task.status = "blocked"
                task.human_request = event.payload.get("human_request")
                task.block_kind = event.payload.get("kind")
                rendered.append(server._format_kanban_event_text(
                    {"task_id": event.task_id, "chat_id": cand["sid"]}, task, event, board))
        return all(isinstance(text, str) for text in rendered) and cand["content"] == "\n".join(rendered)
    except Exception:  # health: allow BLE001 -- optional proof must not turn a source failure into a final error
        return False
