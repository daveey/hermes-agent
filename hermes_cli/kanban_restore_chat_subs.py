"""Rebuild a restored card's hub delivery subscriptions from its synced chat links. [daveey]

``kanban_notify_subs`` is machine-local: the Board carries a card's ``task_chat_links`` but not the ``platform='hub'``
subscription (``<gw>/<profile>/<session>``) its owner wrote with it. Both the notifier (delivery into a chat on
another gateway) and the hermes-hub collector (a card's published ``sessions``) read only that subscription. A worker
reprovisioned from the Board (worker-1, 2026-11-11 ~21:43Z) got its cards and their links back and republished 100 of
215 cards with ``sessions: []``; the hub then saw open cards whose origin chats had vanished (t_01a1cf4cfd7eb125).

The sync apply calls two hooks, both inside its transaction:

- :func:`note_restored_task` when it inserts a card this gateway owns (``created_on`` = it, or unset) into a primary
  live board file. Our own cards are created here, never inserted by the feed (their echo only clears the outbox),
  so such an insert is a restore: the file lost the card (a reprovisioned or rebuilt machine) and the Board gave it
  back. The card is recorded with the restore time in the local-only ``kanban_restored_cards``.
- :func:`restore_link_sub` when it inserts a new ``task_chat_links`` row. For a recorded card, an unreleased link to
  a chat on ANOTHER gateway that existed at the restore (``created_at`` not after it) gets its hub subscription
  back: exact gateway, profile and session, caught up (``last_event_id`` = the card's newest event, no replay), with
  ``reason`` ``origin`` for a ``created`` link (owning: children inherit it) and ``shepherd`` for a ``shepherd`` link.

Never rebuilt: a NULL/empty profile (no exact chat; ``<profile or 'default'>`` would guess, and NULL is never a
wildcard), a released link, a chat on this gateway (a ``tui`` destination, not a hub one), a placeholder or headless
session, an archived card, a link that arrives later for a card this file already held (that chat's subscription may
have been retired on purpose), the replica copy (``sync-shadow``) and link-only files (``sync-links``), and foreign
copies a worker node picks up (their owner delivers). Two links for one chat give one subscription; owning wins.

Known gap: chats on THIS gateway are not rebuilt: a same-gateway chat of another profile (its ``hub`` sub from
``kanban_creator_chats``) and a local ``tui`` shepherd sub have no exact destination in the synced link row.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Optional

MARKER_DDL = ("CREATE TABLE IF NOT EXISTS kanban_restored_cards "
              "(task_id TEXT PRIMARY KEY, restored_at INTEGER NOT NULL)")
# A link written on another gateway just before the restore may carry a slightly later clock.
CLOCK_SKEW_S = 300
_REASON = {"created": "origin", "shepherd": "shepherd"}


def _live_board_file(conn: sqlite3.Connection) -> bool:
    from hermes_cli import kanban_db_sync as sync
    key = sync._db_key(conn)
    return bool(key) and Path(key).parent.name not in (sync.SHADOW_DIR, sync.LINKS_DIR)


def note_restored_task(conn: sqlite3.Connection, task_id: str, created_on: Any, own: str) -> None:
    """Record that the feed just inserted ``task_id``, a card this gateway owns, into this live board file."""
    if created_on not in (None, "", own) or not _live_board_file(conn):
        return
    from hermes_cli import kanban_db_sync as sync
    if sync.board_mode(conn) != "primary":
        return
    conn.execute(MARKER_DDL)
    # The newest restore wins: this only runs when the file lacked the card, so a card lost again after an earlier
    # restore must cover the links written since (keeping the first time would drop them).
    conn.execute("INSERT INTO kanban_restored_cards (task_id, restored_at) VALUES (?, ?) "
                 "ON CONFLICT(task_id) DO UPDATE SET restored_at = excluded.restored_at",
                 (task_id, int(time.time())))


_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def restored_at(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    """Local import time, not a historical event clock; absent on ordinary local cards."""
    if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_restored_cards'").fetchone():
        return None
    row = conn.execute("SELECT restored_at FROM kanban_restored_cards WHERE task_id = ?", (task_id,)).fetchone()
    return int(row[0]) if row else None


def _safe(part: Any) -> bool:
    """One ``<gw>/<profile>/<session>`` segment the hermes-hub collector accepts (its ``parse_shepherd``)."""
    return isinstance(part, str) and bool(_NAME.fullmatch(part))


def restore_link_sub(conn: sqlite3.Connection, cols: Mapping[str, Any], own: str) -> Optional[str]:
    """Write the hub subscription a just-inserted link of a restored card stands for; returns its chat id."""
    task_id = cols.get("task_id")
    if not task_id or not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_restored_cards'").fetchone():
        return None
    marker = conn.execute("SELECT restored_at FROM kanban_restored_cards WHERE task_id = ?", (task_id,)).fetchone()
    if marker is None:
        return None
    gateway, profile, session = cols.get("chat_gateway"), cols.get("chat_profile"), cols.get("chat_session")
    reason = _REASON.get(str(cols.get("kind") or ""))
    if reason is None or cols.get("released") or not gateway or gateway == own:
        return None
    if not all(_safe(p) for p in (gateway, profile, session)):
        return None  # NULL/empty profile included: never widened to 'default'
    if int(cols.get("created_at") or 0) > int(marker[0]) + CLOCK_SKEW_S:
        return None
    status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if status is None or status[0] == "archived":
        return None
    from hermes_cli.kanban_db_notify import is_headless_session
    from hermes_cli.kanban_placement import is_placeholder_session
    chat_id = f"{gateway}/{profile}/{session}"
    if is_placeholder_session(str(session)) or is_headless_session(chat_id, gateway=gateway, profile=profile):
        return None
    metadata = json.dumps({"reason": reason, "restored": "board"}, sort_keys=True, separators=(",", ":"))
    conn.execute(
        "INSERT OR IGNORE INTO kanban_notify_subs "
        "(task_id, platform, chat_id, thread_id, delivery_mode, delivery_metadata, created_at, last_event_id) "
        "VALUES (?, 'hub', ?, '', 'notify', ?, ?, COALESCE((SELECT MAX(id) FROM task_events WHERE task_id = ?), 0))",
        (task_id, chat_id, metadata, int(time.time()), task_id))
    if reason == "origin":  # provenance only upgrades (add_notify_sub's rule): the chat's created link owns it
        conn.execute(
            "UPDATE kanban_notify_subs SET delivery_metadata = json_set(delivery_metadata, '$.reason', 'origin') "
            "WHERE task_id = ? AND platform = 'hub' AND chat_id = ? AND thread_id = '' "
            "AND json_valid(delivery_metadata) AND json_extract(delivery_metadata, '$.reason') = 'shepherd' "
            "AND json_extract(delivery_metadata, '$.restored') = 'board'", (task_id, chat_id))
    return chat_id
