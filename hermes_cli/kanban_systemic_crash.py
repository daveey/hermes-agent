"""Give a systemic crash trip a visible owner: one fix card per fingerprint. [daveey]

The dead-worker sweep trips cards whose crash fingerprint repeats >= 3 times in one tick at once (a retry
would hit the same wall). It used to park them in sticky triage "for an operator" with no card, ask or
owner, so nothing ever looked at them (2026-10-30 12:26 PDT, David-MBP, 3 cards for 35 min).

Now the sweep opens ONE fix card per fingerprint (idempotency key ``systemic-crash:<hash>``, reused across
ticks while it is open), and each tripped card is linked as its child: it waits on the fix card instead of a
sticky hold, and ``recompute_ready`` resumes it when the fix card is done (within the failure budget).
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import Optional

_log = logging.getLogger(__name__)
AUTHOR = "kanban-dispatcher"
_MAX_ROUNDS = 20


def fix_key(fingerprint: str) -> str:
    return "systemic-crash:" + hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:16]


def _open_card(conn: sqlite3.Connection, key: str) -> tuple[Optional[str], bool]:
    """``(id, reusable)`` of the newest non-archived card with ``key``; ``(None, True)`` when none."""
    row = conn.execute(
        "SELECT id, status FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
        "ORDER BY created_at DESC LIMIT 1", (key,)).fetchone()
    if row is None:
        return None, True
    return row["id"], row["status"] != "done"


def ensure_fix_card(conn: sqlite3.Connection, fingerprint: str, task_ids: list[str], sample_error: str,
                    assignee: Optional[str]) -> Optional[str]:
    """The open fix card for ``fingerprint`` (created on first sight), or None when it cannot be made.

    A done fix card did not fix it (the same crash is back): the next round's key (``:r2``...) opens a
    fresh card. Each call names its tripped cards in a comment on the fix card.
    """
    from hermes_cli import kanban_db as kb
    base = fix_key(fingerprint)
    from hermes_cli.kanban_model_neutral import forwarded_text, forwarded_title
    try:
        for n in range(1, _MAX_ROUNDS + 1):
            key = base if n == 1 else f"{base}:r{n}"
            existing, reusable = _open_card(conn, key)
            if existing and not reusable:
                continue
            listed = ", ".join(task_ids)
            if existing:
                kb.add_comment(conn, existing, AUTHOR,
                               f"The same crash tripped {len(task_ids)} more card(s): {listed}. "
                               "They wait on this card as children.")
                return existing
            body = (
                f"{len(task_ids)} Kanban workers crashed with the same error in one dispatcher tick, so the "
                "sweep tripped their cards instead of retrying into the same wall.\n\n"
                "Fingerprint and sample error (historical evidence):\n"
                + forwarded_text(fingerprint) + "\n" + forwarded_text(sample_error[:500]) + "\n\n"
                f"Tripped cards (children of this card): {listed}\n\n"
                "Find and fix the root cause of this crash. When this card is done, the tripped cards resume "
                "on their own (they are linked as its children). Do not re-queue them by hand.")
            return kb.create_task(
                conn, title=forwarded_title(
                    f"Systemic Kanban crash: {len(task_ids)} cards died with `{fingerprint[:60]}`",
                    f"Systemic Kanban crash: {len(task_ids)} cards ({base})"),
                body=body, assignee=assignee, created_by=AUTHOR, idempotency_key=key)
    except (sqlite3.Error, ValueError):
        _log.warning("kanban: opening the systemic-crash fix card failed", exc_info=True)
    return None


def attach(conn: sqlite3.Connection, fix_id: str, task_id: str, *, allow_nested: bool = False) -> bool:
    """Link a tripped card under its fix card and say so on the card. False when the link failed."""
    from hermes_cli import kanban_db as kb
    try:
        kb.link_tasks(conn, fix_id, task_id, **({'allow_nested': True} if allow_nested else {}))
    except (sqlite3.Error, ValueError):
        _log.warning("kanban: linking %s under systemic fix card %s failed", task_id, fix_id, exc_info=True)
        return False
    try:
        kb.add_comment(conn, task_id, AUTHOR,
                       f"Tripped by a systemic crash shared with other cards. Fix card {fix_id} owns it; this "
                       "card resumes when that card is done.")
    except (sqlite3.Error, ValueError):
        _log.debug("kanban: systemic-trip comment failed", exc_info=True)
    return True
