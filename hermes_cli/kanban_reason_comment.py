"""Reason comments that only exist when their state change does, and why a park was refused. [daveey]

``hermes kanban block|schedule|unblock <id> <reason>`` record the reason as a ``PREFIX: reason``
comment. The CLI used to write it in its own transaction before the op ran, so a refused op left
a comment claiming a change that never happened (t_01a18653b77a4f83). :func:`reason_comment`
writes it inside the op's own write txn instead, first (so ``commented`` still precedes the
transition event, which ``escalated_owner`` and the block-loop human-input checks rely on), and
rolls it back to a savepoint unless the op marks success. A crash rolls both back together.
"""
from __future__ import annotations

import contextlib
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional


@dataclass(frozen=True)
class RecordedDecisionComment:
    """Internal verified-operation provenance; never a caller-supplied tool payload.

    The owner adapter must verify the source operation before constructing this.
    It marks an answer, not an operator grant or a transcript message.
    """

    author: str
    body: str
    provenance: dict
    before_insert: Optional[Callable[[sqlite3.Connection], None]] = None


@dataclass(frozen=True)
class FencedReasonComment:
    """Internal pre-insert fence with an ordinary comment, not remote decision provenance."""
    author: str
    body: str
    before_insert: Callable[[sqlite3.Connection], None]


Comment = Optional[tuple[str, str] | RecordedDecisionComment | FencedReasonComment]


class _ReasonComment:
    __slots__ = ("comment_id", "kept")

    def __init__(self) -> None:
        self.comment_id: Optional[int] = None
        self.kept = False

    def keep(self, ok=True):
        """Mark the op successful when ``ok`` is truthy; returns ``ok`` so it wraps a return value."""
        if ok:
            self.kept = True
        return ok


@contextlib.contextmanager
def reason_comment(conn: sqlite3.Connection, task_id: str, comment: Comment) -> Iterator[_ReasonComment]:
    """Use INSIDE the op's ``write_txn``. Writes ``comment`` now and drops it at exit unless
    ``.keep()`` was called; an exception rolls it back with the rest of the txn."""
    from hermes_cli import kanban_db as kb

    state = _ReasonComment()
    decision = comment if isinstance(comment, RecordedDecisionComment) else None
    fenced = comment if isinstance(comment, (RecordedDecisionComment, FencedReasonComment)) else None
    if fenced is not None and fenced.before_insert is not None:
        fenced.before_insert(conn)  # lock held; no tentative duplicate comment yet
    if fenced is not None:
        author, body = comment.author, comment.body
    else:
        author, body = comment or ("", "")
    if not (body and conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()):
        yield state
        return
    # A savepoint, not a DELETE: on a synced board the insert triggers queue outbox rows for the
    # comment and its event, and append-only tables capture no deletes. The refusal paths of
    # block/schedule/unblock write nothing before refusing, so this undoes only the comment.
    savepoint = f"reason_comment_{secrets.token_hex(6)}"
    conn.execute(f"SAVEPOINT {savepoint}")
    extra = ({"recorded_decision": decision.provenance, "writer_kind": "agent"} if decision else {})
    state.comment_id = kb.add_comment(conn, task_id, author, body, **extra)
    try:
        yield state
    except BaseException:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        raise
    if not state.kept:
        conn.execute(f"ROLLBACK TO {savepoint}")
        state.comment_id = None
    conn.execute(f"RELEASE {savepoint}")


def _awaiting_human_answer(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when the card's outstanding ask is for a human: a human-kind ``block_kind``
    (``needs_input``/``capability``) whose latest ``blocked``/``block_loop_detected`` event
    has had no ``unblocked`` and no new run (``claimed``) since. [daveey]"""
    from hermes_cli import kanban_db as kb

    row = conn.execute("SELECT status, block_kind FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or kb._row_get(row, "block_kind") not in ("needs_input", "capability"):
        return False
    ask = conn.execute(
        "SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind IN ('blocked', 'block_loop_detected')",
        (task_id,)).fetchone()[0]
    if ask is None:
        return row["status"] == "blocked"
    answered = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND id > ? AND kind IN ('unblocked', 'claimed') LIMIT 1",
        (task_id, ask)).fetchone()
    return answered is None


def human_answer_refusal(conn: sqlite3.Connection, task_id: str, op: str = "schedule") -> Optional[str]:
    """Why ``op`` was refused because the card waits on a human answer (``_awaiting_human_answer``),
    with the way out; None when that guard does not apply."""
    if not _awaiting_human_answer(conn, task_id):
        return None
    row = conn.execute("SELECT block_kind FROM tasks WHERE id = ?", (task_id,)).fetchone()
    ts = conn.execute(
        "SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind IN ('blocked', 'block_loop_detected')",
        (task_id,)).fetchone()[0]
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(ts))) if ts else "at an unrecorded time"
    gerund = "scheduling" if op == "schedule" else f"{op}ing"
    return (f"cannot {op} {task_id}: it is blocked on a human answer (block_kind {row[0]}, blocked {when}); "
            f"{gerund} would hide that ask. Move the ask to a card the human sees, then: "
            f'hermes kanban unblock {task_id} --reason "..." && hermes kanban {op} {task_id} ...')
