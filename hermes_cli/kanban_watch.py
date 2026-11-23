"""Watch parks: a later check that needs nothing from its chat does not hold the chat open. [daveey]

A card parked ``scheduled`` counts as its chat's open work (``kanban_session_activity.ACTIVE_STATUSES``), so a
"check this again on Friday" card kept a chat unfinished for days although nothing needed David (self-heal
kanban-85e428ecb2). ``kanban_schedule(watch=true)`` stamps the park's ``scheduled`` event ``watch: true``. While the
card is ``scheduled`` and its newest park event is that watch, the session counts, ``session_tasks`` (the close-out
gate) and the parked counts leave it out. Nothing else changes: the chat's notify subscription stays, so a later
block, failure or done still reaches it, and once the card wakes it counts again like any running card.

The flag is sticky: a re-park without ``watch`` keeps the previous park's answer; ``watch=false`` clears it.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

# A park further out than this, holding an interactive chat, gets the ``chat_hold`` hint.
HINT_AFTER_S = 24 * 3600
# ``kanban_schedule`` result for a watch park: what the chat's row shows, and what to say when asked about it.
WATCH_NOTE = ("The chat shows done with a watch mark naming this card until it wakes. Asked about the chat's state, "
              "name this pending check; do not say everything is finished.")


def _park_kinds_sql() -> str:
    from hermes_cli.kanban_wait_info import PARK_EVENT_KINDS
    return ", ".join(f"'{k}'" for k in PARK_EVENT_KINDS)


def watch_park_sql(alias: str = "t") -> str:
    """SQL predicate: the card (alias ``alias``) is parked ``scheduled`` as a watch."""
    return (f"({alias}.status = 'scheduled' AND EXISTS (SELECT 1 FROM task_events we WHERE we.id = "
            f"(SELECT MAX(id) FROM task_events WHERE task_id = {alias}.id AND kind IN ({_park_kinds_sql()})) "
            f"AND we.kind = 'scheduled' AND CASE WHEN json_valid(we.payload) "
            f"THEN json_extract(we.payload, '$.watch') ELSE 0 END = 1))")


def not_watch_sql(conn: sqlite3.Connection, alias: str = "t") -> str:
    """``NOT watch_park_sql`` on boards whose ``task_events`` carry park payloads, else ``1`` (no watches)."""
    try:
        from hermes_cli.kanban_table_columns import table_columns
        cols = table_columns(conn, "task_events")  # one result row, not one per column
    except sqlite3.Error:
        cols = set()
    return f"NOT {watch_park_sql(alias)}" if {"id", "task_id", "kind", "payload"} <= cols else "1"


def _last_scheduled(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'scheduled' "
                       "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[0]) if row[0] else {}
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve(conn: sqlite3.Connection, task_id: str, requested: Any) -> bool:
    """The watch flag for a new park: the explicit ``requested`` bool, else the previous park's."""
    if requested is not None:
        return bool(requested)
    prev = _last_scheduled(conn, task_id)
    return bool(prev and prev.get("watch") is True)


def is_watch(conn: sqlite3.Connection, task_id: str) -> bool:
    try:
        return bool(conn.execute(f"SELECT 1 FROM tasks t WHERE t.id = ? AND {watch_park_sql('t')}",
                                 (task_id,)).fetchone())
    except sqlite3.Error:
        return False


def held_chats(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Interactive chats this card counts as open work for: its creator and its counted ``tui`` subscribers."""
    from hermes_cli import kanban_db_notify as kbn
    chats: list[str] = []
    row = conn.execute("SELECT session_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row and row[0]:
        chats.append(str(row[0]))
    try:
        for (chat, meta) in conn.execute(
                "SELECT chat_id, delivery_metadata FROM kanban_notify_subs WHERE task_id = ? AND platform = 'tui' "
                "AND chat_id != ''", (task_id,)):
            try:
                reason = (json.loads(meta) or {}).get("reason") if meta else None
            except (TypeError, ValueError, AttributeError):
                reason = None
            if reason not in kbn.UNCOUNTED_SUB_REASONS:  # touch / send_refused are notice-only [daveey]
                chats.append(str(chat))
    except sqlite3.Error:
        pass
    chats = [c for c in dict.fromkeys(chats) if not kbn.is_headless_session(c)]
    if not chats:
        return []
    try:
        from hermes_cli.kanban_origin import gateway_names
        me = gateway_names()[0]
        machine = kbn.machine_sessions(me, chats, local_gateway=me) if me else set()
    except (sqlite3.Error, OSError, ValueError):  # unreadable hub/state db: treat every chat as interactive
        machine = set()
    return [c for c in chats if c not in machine and not c.rsplit("/", 1)[-1].startswith("cron_")]


def _read_chat_watches(conn: sqlite3.Connection, ids: list[str], counted_sub: str) -> list[dict]:
    """``{id, title, wake_at}`` per watch-parked card on one board that ``ids`` created or counts as a ``tui``
    subscriber of: the cards ``kanban_session_activity`` leaves out of the chat's open work."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    events = {r[1] for r in conn.execute("PRAGMA table_info(task_events)")}
    if "session_id" not in cols or not {"id", "task_id", "kind", "payload"} <= events:
        return []
    ph = ",".join("?" for _ in ids)
    found: dict[str, str] = {}
    for tid, title in conn.execute(
            f"SELECT t.id, t.title FROM tasks t WHERE t.session_id IN ({ph}) AND {watch_park_sql('t')}", ids):
        found[tid] = title
    # A board that predates notify subscriptions has no table. Any other read error (locked, busy) propagates to
    # the caller's guard, which logs it, instead of silently reading as "no subscriber watches".
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'kanban_notify_subs'").fetchone():
        for tid, title in conn.execute(
                f"SELECT t.id, t.title FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
                f"WHERE s.platform = 'tui' AND {counted_sub} AND s.chat_id IN ({ph}) AND {watch_park_sql('t')}", ids):
            found.setdefault(tid, title)
    out = []
    for tid, title in found.items():
        wake = (_last_scheduled(conn, tid) or {}).get("wake_at")
        out.append({"id": tid, "title": title,
                    "wake_at": float(wake) if isinstance(wake, (int, float)) and not isinstance(wake, bool) else None})
    return out


def chat_watches(session_ids: list[str]) -> list[dict]:
    """The chat's watch-parked cards on this gateway's boards, as outcome items
    ``{kind: "card", id, label, state: "watching", eta?}``. A watch frees the chat (it is not open work), so a done
    chat names them like the crons it owns, instead of reading as "nothing left" (self-heal desktop-e6cce5965c).
    Failed board reads propagate; the outcome guards this read like the cron one."""
    from hermes_cli import kanban_session_activity as ksa
    ids = [s for s in dict.fromkeys(str(s) for s in session_ids if s)]
    if not ids:
        return []
    items, seen = [], set()
    for path in ksa._board_db_paths():
        with ksa.cached_ro(path) as conn:
            rows = _read_chat_watches(conn, ids, ksa._counted_sub_clause(conn))
        for card in rows:
            if card["id"] in seen:
                continue
            seen.add(card["id"])
            item = {"kind": "card", "id": card["id"], "label": card["title"] or card["id"], "state": "watching"}
            if card["wake_at"] is not None:
                item["eta"] = card["wake_at"]
            items.append(item)
    return items


def chat_hold_hint(conn: sqlite3.Connection, task_id: str, *, now: Optional[float] = None) -> Optional[str]:
    """For a non-watch park more than ``HINT_AFTER_S`` out that holds an interactive chat: say which and until when."""
    payload = _last_scheduled(conn, task_id) or {}
    if payload.get("watch") is True:
        return None
    wake = payload.get("wake_at")
    now = time.time() if now is None else now
    if not isinstance(wake, (int, float)) or wake - now <= HINT_AFTER_S:
        return None
    chats = held_chats(conn, task_id)
    if not chats:
        return None
    until = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(wake))
    return (f"This keeps chat {', '.join(chats)} unfinished until {until}. If nothing is needed from David "
            f"before then, re-park it with watch=true.")


NOT_RUNNING_TEMPLATE = (
    "could not complete {tid}: card is {status} with no run; to close it unverified, archive it with a comment; "
    "to keep the check but free the chat, re-park it with watch=true")
TRIAGE_NOT_RUNNING_TEMPLATE = (
    "could not complete {tid}: card is triage with no run (not yet specified/promoted); "
    "promote it to ready or archive it with a comment")
# Statuses where a refused kanban_complete really is a stale run: the card runs (or waits in
# blocked/review) under another run, or is already terminal. [daveey]
STALE_RUN_STATUSES = frozenset({"running", "blocked", "review", "done", "archived"})


def not_running_reason(tid: str, status: str) -> Optional[str]:
    """Refusal text naming ``status`` for a card with no run, or None when the refusal is a
    stale run. Classified by the stale-run statuses, not a list of waiting ones, so a status
    added later is named instead of mislabeled "stale run" (triage was, after todo and
    scheduled). [daveey]"""
    if status in STALE_RUN_STATUSES:
        return None
    if status == "triage":
        return TRIAGE_NOT_RUNNING_TEMPLATE.format(tid=tid)
    return NOT_RUNNING_TEMPLATE.format(tid=tid, status=status)
