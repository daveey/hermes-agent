"""Which chats a card's change reaches, and what a change does to the chat's earlier notices. [daveey]

Session-outcome contract, "Fan-out" and "Stale notices":

- A notice or a card-change refresh never goes to a chat whose link to the card is released, nor to a chat row
  that is archived or was handed to another gateway (``end_reason`` ``moved_to_gateway:<gw>``, the copy left
  behind by ``session_handover``). One block once reached five chats: the creator, a shepherd, a released link
  and two moved copies (session-state audit 2026-10-29, ``t_e5cfb7c4``).
- A notice is a snapshot of the card when it was written. When the card leaves ``blocked``, its earlier
  "blocked" notice rows in the chat get ``display_metadata.stale = true`` (the Desktop dims them) and the
  card's id joins ``display_metadata.stale_tasks``; a row about several cards is stale once all of them moved on.
- :func:`card_change_pass` (run from the unloaded sweeper's loop, every backend) reads each board's new status
  events since the last pass and, for every chat linked to a changed card, marks those notices and refreshes the
  chat's stored outcome (trigger ``card_change``). The first pass of a process only seeds its cursor; the other
  refresh points (turn end, notice write, reap) cover what changed while no backend ran. Every step is
  idempotent, so two backends running the pass at once write the same result.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

MOVED_PREFIX = "moved_to_gateway:"
# Task events that can change a card's status (and so a linked chat's outcome). Chatter (heartbeats, comments,
# attachments, model tiers) is not read: thousands a day.
STATUS_EVENT_KINDS = (
    "created", "blocked", "unblocked", "scheduled", "promoted", "promoted_manual", "claimed", "reclaimed",
    "spawned", "completed", "archived", "gave_up", "crashed", "timed_out", "review_requested",
    "changes_requested", "review_reopened", "reopened", "status", "dependency_wait", "triage_swept", "moved",
    "continued", "continuation_woken", "condition_met",
)
MAX_EVENTS_PER_PASS = 2000
_BLOCKED_LINE = re.compile(r"Kanban (t_[0-9A-Za-z_]+) blocked")
_TASK_ID = re.compile(r"Kanban (t_[0-9A-Za-z_]+)")
_cursors: dict[str, int] = {}


def _reset_for_tests() -> None:
    _cursors.clear()


def chat_skip_reason(db: Any, chat_id: str) -> Optional[str]:
    """``"archived"`` / ``"moved"`` when no notice or refresh should reach ``chat_id`` here, else None. The row
    and its compression tip are both checked (archive flips a whole lineage; a move ends the tip)."""
    ids = list(dict.fromkeys(i for i in (chat_id, db.get_compression_tip(chat_id)) if i))
    marks = ",".join("?" * len(ids))
    for row in db._read_all(f"SELECT archived, end_reason FROM sessions WHERE id IN ({marks})", tuple(ids)):
        if str(row["end_reason"] or "").startswith(MOVED_PREFIX):
            return "moved"
        if row["archived"]:
            return "archived"
    return None


WORKER_TRANSCRIPT = "worker_transcript"


def worker_transcript_reason(db: Any, chat_id: str) -> Optional[str]:
    """``"worker_transcript"`` when ``chat_id`` is a headless run, not a chat: a ``local-<sha256>`` receipt id
    (Kanban worker, cron, one-shot) or a session this store tags ``kanban`` (older worker ids look like chats).
    A notice there is never read and its wake is always refused (``session.resume`` code 4132). [daveey]

    Hub card t_a989e5e0 (2026-10-31): five MacBook worker transcripts of another card held shepherd subs on it, so
    its done notice was written into all five and each wake was refused. Never raises (unknown = a chat)."""
    from hermes_cli.kanban_session_provenance import _HEADLESS_SESSION, _INTERACTIVE_SOURCES, session_creation_source
    from hermes_cli.kanban_session_provenance import MACHINE_SESSION_SOURCES
    # The supplied store is the destination authority, not the process's ambient profile.
    read = getattr(db, "get_session", None)
    try:
        row = read(chat_id) if read is not None else None
    except sqlite3.Error:
        row = None
    source = session_creation_source(dict(row)) if isinstance(row, (dict, sqlite3.Row)) else None
    if source in MACHINE_SESSION_SOURCES or (
            _HEADLESS_SESSION.fullmatch(chat_id) and source not in _INTERACTIVE_SOURCES):
        return WORKER_TRANSCRIPT
    probe = getattr(db, "is_kanban_owned_session", None)
    try:
        return WORKER_TRANSCRIPT if probe is not None and probe(chat_id) is True else None
    except sqlite3.Error:
        logger.debug("worker-transcript probe failed for %s", chat_id, exc_info=True)
        return None


def link_released(link: Any) -> bool:
    """A replica ``sessions[]`` link (or ``task_chat_links`` row) the owner released: no notices go there."""
    return isinstance(link, dict) and str(link.get("released") or "0").lower() not in ("0", "false", "")


def _decode(raw: Any) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _notice_tasks(content: str, meta: dict) -> set[str]:
    ids = {str(t) for t in (meta.get("task_ids") or ()) if t}
    if meta.get("task_id"):
        ids.add(str(meta["task_id"]))
    return ids | set(_TASK_ID.findall(content or ""))


def mark_stale_blocked_notices(db: Any, sid: str, task_id: str) -> int:
    """Mark the chat lineage's earlier "blocked" notices about ``task_id`` as outdated. Returns rows changed."""
    from hermes_state_sessions import _LINEAGE_CTE_SQL
    rows = db._read_all(
        _LINEAGE_CTE_SQL + " SELECT id, content, display_metadata FROM messages WHERE session_id IN "
        "(SELECT id FROM lineage) AND display_kind = 'kanban_notice' AND content LIKE ?",
        (sid, sid, f"%Kanban {task_id} blocked%"))
    changed = 0
    for row in rows:
        content = str(row["content"] or "")
        if task_id not in _BLOCKED_LINE.findall(content):
            continue
        meta = _decode(row["display_metadata"])
        stale_tasks = sorted({*(meta.get("stale_tasks") or ()), task_id})
        stale = set(stale_tasks) >= _notice_tasks(content, meta)
        if meta.get("stale_tasks") == stale_tasks and bool(meta.get("stale")) == stale:
            continue
        meta.update(stale_tasks=stale_tasks, **({"stale": True} if stale else {}))
        changed += db._write_rowcount("UPDATE messages SET display_metadata = ? WHERE id = ? AND display_metadata IS ?",
                                      (json.dumps(meta, ensure_ascii=False), row["id"], row["display_metadata"]))
    return changed


def _linked_chats(conn: sqlite3.Connection, task_id: str, created_by: Optional[str]) -> list[str]:
    """Chats linked to the card on this board: its creating chat and its ``tui`` subscribers, minus chats
    whose every ``task_chat_links`` row for the card is released."""
    chats = [created_by] if created_by else []
    chats += [str(r[0]) for r in conn.execute(
        "SELECT chat_id FROM kanban_notify_subs WHERE task_id = ? AND LOWER(platform) = 'tui' AND chat_id != ''",
        (task_id,))]
    released: set[str] = set()
    with contextlib.suppress(sqlite3.Error):  # boards that never synced have no link table
        rows = conn.execute("SELECT chat_session, MIN(released) FROM task_chat_links WHERE task_id = ? "
                            "GROUP BY chat_session", (task_id,)).fetchall()
        released = {str(r[0]) for r in rows if r[1]}
    return [c for c in dict.fromkeys(chats) if c not in released]


def _changed_tasks(conn: sqlite3.Connection, ident: str) -> list[str]:
    top = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0])
    since = _cursors.get(ident)
    _cursors[ident] = top
    if since is None or top <= since:
        return []
    kinds = ",".join("?" * len(STATUS_EVENT_KINDS))
    rows = conn.execute(f"SELECT task_id FROM task_events WHERE id > ? AND id <= ? AND kind IN ({kinds}) "
                        f"ORDER BY id DESC LIMIT ?", (since, top, *STATUS_EVENT_KINDS, MAX_EVENTS_PER_PASS))
    return list(dict.fromkeys(str(r[0]) for r in rows))


def apply_card_change(db: Any, tip: str, chat_id: str, task_id: str, status: str) -> Optional[str]:
    """One linked chat's share of a card change: stale notices when the card is not blocked, then a refresh.
    Returns why the chat was skipped, else None."""
    from hermes_state_outcome import refresh_outcomes_quietly
    if (why := chat_skip_reason(db, chat_id)) is not None:
        return why
    if status != "blocked":
        with contextlib.suppress(Exception):
            mark_stale_blocked_notices(db, tip, task_id)
    refresh_outcomes_quietly([tip], "card_change", db=db)
    return None


def _fan_out_change(conn: sqlite3.Connection, _kb: Any, session_db: Callable, slug: str, tid: str) -> list[dict]:
    task = _kb.get_task(conn, tid)
    status = str(getattr(task, "status", "") or "")
    out = []
    for chat in _linked_chats(conn, tid, getattr(task, "session_id", None)):
        with session_db(chat) as (db, tip):
            if db is None:
                continue
            skipped = apply_card_change(db, tip, chat, tid, status)
        out.append({"board": slug, "task_id": tid, "chat_id": chat, "session_id": tip, "status": status,
                    "skipped": skipped})
    return out


def card_change_pass(*, session_db: Optional[Callable] = None, slugs: Optional[Iterable[str]] = None) -> list[dict]:
    """Refresh every chat linked to a card whose status moved since the last pass (see module doc). Never
    raises; returns ``[{board, task_id, chat_id, session_id, status, skipped}]``."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    from tui_gateway import kanban_unloaded as ku
    session_db = session_db or ku.find_session_db
    done: list[dict] = []
    for slug in (list(slugs) if slugs is not None else ku._board_slugs(_kb)):
        try:
            with contextlib.closing(_kbc.connect(board=slug)) as conn:
                ident = str(_kb.kanban_db_path(slug).resolve())
                for tid in _changed_tasks(conn, ident):
                    done += _fan_out_change(conn, _kb, session_db, slug, tid)
        except Exception:
            logger.warning("card-change outcome pass failed on board %s", slug, exc_info=True)
    return done
