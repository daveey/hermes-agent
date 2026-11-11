"""Tell a waiting chat when the parent its card waits on sits ready and unclaimed. [daveey]

Defect kanban-447c98343f (2026-11-02): a rollout card parked at 19:48 on its parent, which sat ready from 19:04 to
21:04 without a worker; the chat that owned the rollout heard nothing and David asked "what are we waiting for"
three times. The dispatcher now runs a card others wait on first (``DISPATCH_ORDER``), but a busy or capped host can
still leave it queued; this module makes that visible to the chat instead of to David.

:func:`sweep` (run by every non-dry dispatcher tick) appends ONE ``dependency_parent_unclaimed`` event per wait to
a chat-owned ``todo`` child (it has a notify subscription or a ``session_id``) whose latest ``dependency_wait`` is
at least :data:`THRESHOLD_SECONDS` old while a parent has been ready and unclaimed that long. The gateway and TUI
notifiers deliver the event to the child's chats with :func:`notice_text`. :func:`ready_hint` is the matching
``kanban_show`` line for the parent itself.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

_log = logging.getLogger(__name__)

NOTICE_KIND = "dependency_parent_unclaimed"
THRESHOLD_SECONDS = 30 * 60
NO_FIX = "a card that can't start is no fix"


def _minutes(seconds: float) -> int:
    return max(0, int(seconds // 60))


def ready_since(conn, task_id: str, created_at: Optional[int] = None) -> int:
    """When ``task_id`` last entered ``ready`` (the stranded-in-ready diagnostic's floor, its latest run's end
    included), else its creation."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_diagnostics import _last_ready_entry_ts
    ended = conn.execute("SELECT MAX(ended_at) AS ended_at FROM task_runs WHERE task_id = ?", (task_id,)).fetchall()
    since = _last_ready_entry_ts(kb.list_events(conn, task_id), [dict(r) for r in ended])
    return int(since or created_at or 0)


def _place(conn, parent_id: str) -> dict[str, Any]:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_queue_position as kqp
    try:
        return kqp.position(conn, kb.get_task(conn, parent_id)) or {}
    except Exception:  # health: allow BLE001 -- a queue place is advisory; the notice still goes out
        _log.debug("parent-unclaimed: queue position unavailable for %s", parent_id, exc_info=True)
        return {}


def _queue_words(place: dict[str, Any]) -> str:
    if place.get("lane") == "worker_pool":
        return f"waiting for a pool worker ({place.get('reason') or 'no reason recorded'})"
    ahead = place.get("ahead")
    words = f"{ahead} card(s) ahead of it" if ahead is not None else "queue place unknown"
    if place.get("running") is not None and place.get("limit") is not None:
        words += f", {place['running']}/{place['limit']} worker slots busy"
    return words


def _options(parent_id: str) -> str:
    return (f"{NO_FIX}: bump its priority (`hermes kanban edit {parent_id} --priority 10`), re-place it on a "
            f"gateway with capacity, or do the work here")


def notice_text(payload: Any) -> str:
    """Chat line for the event (gateway and TUI notifiers share it)."""
    payload = payload if isinstance(payload, dict) else {}
    parent = str(payload.get("parent") or "<parent>")[:64]
    title = str(payload.get("parent_title") or "").strip()[:120]
    named = f"{parent} ({title})" if title else parent
    return (f"Waiting {payload.get('wait_minutes', '?')} min on parent {named}, which has been ready "
            f"{payload.get('ready_minutes', '?')} min with no worker ({payload.get('queue') or 'queue place unknown'}). "
            + _options(parent) + ".")


def ready_hint(conn, task, *, now: Optional[float] = None) -> Optional[str]:
    """``kanban_show`` hint for a card ready and unclaimed at least :data:`THRESHOLD_SECONDS`; None otherwise."""
    if task is None or task.status != "ready" or getattr(task, "claim_lock", None):
        return None
    now = time.time() if now is None else now
    waited = now - ready_since(conn, task.id, task.created_at)
    if waited < THRESHOLD_SECONDS:
        return None
    return f"Ready {_minutes(waited)} min, unclaimed; {_queue_words(_place(conn, task.id))}. " + _options(task.id) + "."


_CANDIDATES = (
    "SELECT c.id AS child, p.id AS parent, p.title AS parent_title, p.created_at AS parent_created "
    "FROM tasks c JOIN task_links l ON l.child_id = c.id JOIN tasks p ON p.id = l.parent_id "
    "WHERE c.status = 'todo' AND p.status = 'ready' AND p.claim_lock IS NULL "
    "AND (c.session_id IS NOT NULL OR EXISTS (SELECT 1 FROM kanban_notify_subs s WHERE s.task_id = c.id)) "
    "ORDER BY c.id, p.id"
)


def sweep(conn, *, now: Optional[float] = None) -> int:
    """Append the once-per-wait notice for every qualifying (child, parent); returns how many were appended."""
    from hermes_cli import kanban_db as kb
    now = time.time() if now is None else now
    posted = 0
    for row in conn.execute(_CANDIDATES).fetchall():
        wait = conn.execute(
            "SELECT id, created_at FROM task_events WHERE task_id = ? AND kind = 'dependency_wait' "
            "ORDER BY id DESC LIMIT 1", (row["child"],)).fetchone()
        if wait is None:
            continue
        waited = now - (kb._to_epoch(wait["created_at"]) or now)
        ready_for = now - ready_since(conn, row["parent"], row["parent_created"])
        if waited < THRESHOLD_SECONDS or ready_for < THRESHOLD_SECONDS:
            continue
        if post_notice(conn, row["child"], row["parent"], wait_key=wait["id"], waited_seconds=waited,
                       ready_seconds=ready_for, parent_title=row["parent_title"]):
            posted += 1
    return posted


def safe_sweep(conn) -> int:
    """:func:`sweep` for the dispatcher tick, then the direct chat-wait sweep (``kanban_chat_wait``, same
    cadence; kept here so the dispatcher facade does not grow). A notice failure is logged and never stops
    dispatch."""
    from hermes_cli import kanban_chat_wait
    try:
        posted = sweep(conn)
    except Exception:  # health: allow BLE001 -- a notice failure must never stop dispatch
        _log.warning("kanban dispatcher: parent-unclaimed notice sweep failed", exc_info=True)
        posted = 0
    return posted + kanban_chat_wait.safe_sweep(conn)


def post_notice(conn, task_id: str, waiting_on: str, *, wait_key: Any, waited_seconds: float,
                ready_seconds: float, parent_title: Optional[str] = None, kind: str = NOTICE_KIND,
                extra: Optional[dict[str, Any]] = None) -> bool:
    """Append ONE ``kind`` event (default ``dependency_parent_unclaimed``) to ``task_id`` (its chats receive it
    through their notify subscriptions) about ``waiting_on``, a card still ready and unclaimed. ``wait_key``
    identifies the wait: a second call with the same (waiting_on, wait_key) posts nothing. Reused for a direct chat
    wait (``kanban_chat_wait``), where ``waiting_on`` is ``task_id`` itself; ``extra`` adds payload fields (its
    clock hold). Returns True when it posted."""
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        if any(kb._json_dict(p).get("wait_event_id") == wait_key and kb._json_dict(p).get("parent") == waiting_on
               for (p,) in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                                        (task_id, kind))):
            return False
        still = conn.execute("SELECT status, claim_lock FROM tasks WHERE id = ?", (waiting_on,)).fetchone()
        if still is None or still["status"] != "ready" or still["claim_lock"]:
            return False
        kb._append_event(conn, task_id, kind, {
            "parent": waiting_on, "parent_title": parent_title, "wait_event_id": wait_key,
            "wait_minutes": _minutes(waited_seconds), "ready_minutes": _minutes(ready_seconds),
            "queue": _queue_words(_place(conn, waiting_on)), **(extra or {}),
        })
    return True
