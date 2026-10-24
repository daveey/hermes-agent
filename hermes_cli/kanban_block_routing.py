"""Block transition policy, separate from the database facade."""
from __future__ import annotations


def _normalized_reason(reason: str | None) -> str:
    return " ".join((reason or "").split()).casefold()


def route_block(
    kind: str | None, reason: str | None, source_status: str, *,
    prev_kind: str | None, prev_recurrences: int, prev_reason: str | None = None,
    compare_reason: bool = False,
) -> tuple[str, str, str, tuple, dict]:
    """Return status, event, SQL fields/params and payload for a block.

    Dependency waits never enter the human bucket. Repeated blocks retain the
    loop breaker; a changed human question resets it because an answer is progress.
    """
    from hermes_cli import kanban_db as kb

    payload = {"reason": reason, "kind": kind, "source_status": source_status}
    if kind == "dependency":
        return "todo", "dependency_wait", "block_kind    = ?", (kind,), payload
    same_cause = prev_kind == kind and (
        not (compare_reason and kind == "needs_input")
        or _normalized_reason(reason) == _normalized_reason(prev_reason)
    )
    recurrences = prev_recurrences + 1 if same_cause else 1
    set_sql = "block_kind    = ?,\n                       block_recurrences = ?"
    payload = {"reason": reason, "kind": kind, "recurrences": recurrences, "source_status": source_status}
    if recurrences >= kb.BLOCK_RECURRENCE_LIMIT:
        payload["limit"] = kb.BLOCK_RECURRENCE_LIMIT
        return "triage", "block_loop_detected", set_sql, (kind, recurrences), payload
    return "blocked", "blocked", set_sql, (kind, recurrences), payload
