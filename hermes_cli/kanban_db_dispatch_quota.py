"""Quota walls clear on their own, so the respawn guard never turns one into a human block. [daveey]

2026-10-23: six cards sat ``blocked`` / ``kind=capability`` for ~13 h after a quota wall that had
already cleared. The rate-limit requeue stamps ``last_failure_error`` with "exited rate-limited
(quota wall)". A later non-failure run (``changes_requested``, ``blocked``...) then became the
latest run, so the ``outcome == 'rate_limited'`` cooldown branch no longer fired, and the stamp
matched ``_RESPAWN_BLOCKER_RE`` as ``blocker_auth``. After the 30-min hold cap the dispatcher blocked
the card as ``capability``, a human kind, so the owning chats painted red.

The rules here:

* :func:`is_quota_error` tells quota / rate-limit text from auth text. Auth wins when both appear
  (a 401 next to a 429 still needs a person).
* :func:`clear_quota_error` drops the stamp once the cooldown is over, so it cannot re-trap the card.
* :func:`release_recovered_quota_blocks` releases blocks the old code already made: a guard-hold
  block whose recorded error is a quota wall, once another card has completed since the block
  (proof the provider is answering again).

Split out of ``kanban_db_dispatch`` (code-health file cap).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Optional

_QUOTA_RE = re.compile(
    r"\b(quota|rate[\s_\-]?limit(?:ed|s|ing)?|429|too[\s_]many[\s_]requests)\b",
    re.IGNORECASE,
)

# The auth half of ``kanban_db_dispatch._RESPAWN_BLOCKER_RE``: a person (or a credential refresh)
# has to act. ``billing``/``subscription`` stay here because an empty balance does not refill itself.
_AUTH_RE = re.compile(
    r"\b(401|403|auth|authenticat(?:e|es|ed|ing|ion)|authoriz(?:e|es|ed|ing|ation)|"
    r"authoris(?:e|es|ed|ing|ation)|authz|unauthorized|forbidden|billing|subscription|"
    r"access[\s_]denied|permission[\s_]denied|invalid[\s_]api[\s_]key)\b",
    re.IGNORECASE,
)

GUARD_HOLD_BLOCK_PREFIX = "Held in ready by the respawn guard ("
# Guard reasons whose hold-cap block a recovered quota wall may release.
_RELEASABLE_GUARD_REASONS = ("blocker_auth", "rate_limit_cooldown")
_HELD_RE = re.compile(
    r"^Held in ready by the respawn guard \((?P<reason>[a-z_]+)\) for \d+ min without running\. "
    r"Last error: (?P<error>.*?)(?: Fix the cause\b.*)?$",
    re.DOTALL,
)
RELEASE_NOTE_MARKER = "[respawn-guard quota release]"


def is_quota_error(err: Optional[str]) -> bool:
    """True when ``err`` reads as a quota wall / rate limit and carries no auth signal."""
    return bool(err) and bool(_QUOTA_RE.search(err)) and not _AUTH_RE.search(err)


def clear_quota_error(conn: sqlite3.Connection, task_id: str, err: str) -> None:
    """Drop a stale quota stamp so it cannot re-trap the card (compare-and-swap on the text)."""
    from hermes_cli import kanban_db as _kb

    with _kb.write_txn(conn, allow_nested=True):
        from hermes_cli.kanban_home_transfer import frozen
        if frozen(conn, task_id):
            return
        cur = conn.execute(
            "UPDATE tasks SET last_failure_error = NULL WHERE id = ? AND last_failure_error = ?",
            (task_id, err),
        )
        if cur.rowcount:
            _kb._append_event(conn, task_id, "quota_error_cleared", {"error": err[:300]})


def _parse_hold_block(reason: Optional[str]) -> Optional[tuple[str, str]]:
    if not reason or not reason.startswith(GUARD_HOLD_BLOCK_PREFIX):
        return None
    m = _HELD_RE.match(reason.strip())
    if m is None:
        return None
    return m.group("reason"), m.group("error").strip()


def release_recovered_quota_blocks(conn: sqlite3.Connection) -> list[str]:
    """Unblock guard-hold blocks that were really quota walls, once the wall has cleared.

    A card qualifies when it is ``blocked``/``triage`` with a human ``block_kind``
    (``capability``/``needs_input``), its newest block reason is the respawn guard's hold-cap
    sentence for ``blocker_auth``/``rate_limit_cooldown``, the error in it is quota-flavored
    (:func:`is_quota_error`), and some OTHER card has a ``completed`` run that ended after the
    block. Each release gets a comment saying why.
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli.kanban_wake_check import foreign_ids

    # A retained executor copy may own a local probe, not this maintenance decision.
    # Only the card's home releases it; replica-local dedup cannot fence another gateway.
    copies = foreign_ids(conn)
    from hermes_cli.kanban_home_transfer import frozen
    released: list[str] = []
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status IN ('blocked', 'triage') "
        "AND block_kind IN ('capability', 'needs_input')"
    ).fetchall()
    for row in rows:
        task_id = row["id"]
        if task_id in copies or frozen(conn, task_id):
            continue
        from hermes_cli.kanban_block_routing import latest_block_reason

        parsed = _parse_hold_block(latest_block_reason(conn, task_id))
        if parsed is None:
            continue
        guard_reason, err = parsed
        if guard_reason not in _RELEASABLE_GUARD_REASONS or not is_quota_error(err):
            continue
        blocked_at = conn.execute(
            "SELECT MAX(created_at) FROM task_events WHERE task_id = ? "
            "AND kind IN ('blocked', 'block_loop_detected')", (task_id,),
        ).fetchone()[0]
        if blocked_at is None:
            continue
        proof = conn.execute(
            "SELECT task_id, ended_at FROM task_runs WHERE outcome = 'completed' AND task_id != ? "
            "AND ended_at > ? ORDER BY ended_at LIMIT 1", (task_id, int(blocked_at)),
        ).fetchone()
        if proof is None:
            continue
        if not _kb.unblock_task(conn, task_id):
            continue
        _kb.add_comment(
            conn, task_id, "kanban-dispatcher",
            f"{RELEASE_NOTE_MARKER} Released automatically. This block was the respawn guard's "
            f"hold cap ({guard_reason}) on a quota/rate-limit error ({err[:200]}), which clears on its "
            f"own and never needed a person. Card {proof['task_id']} completed after the block, so the "
            f"provider is answering again; back to ready.",
        )
        released.append(task_id)
    return released
