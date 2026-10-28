"""Which chats a card belongs to, synced through the Board (RFC "Retiring the origin ledger", phase 5). [daveey]

``task_chat_links`` rows (card, chat gateway/profile/session, kind ``created``/``shepherd``, ``released``) travel
like any synced row, so every gateway's replica can answer "which cards does this chat have", on every gateway,
with one local query.

- :func:`reconcile` (run by the sync loop, idempotent, so its first run is the backfill) writes this gateway's
  links into a board file: chats that created a card here (``tasks.session_id``), chats that shepherd a card here
  (``tui`` subscriptions), and this gateway's chats' links to other gateways' cards from the origin-ledger
  projection, with its released state. Links it no longer finds are marked released, never deleted.
- :func:`replica_cards` answers a chat lineage's cards on other gateways from the replicas; ``session_tasks`` uses
  it instead of the origin ledger when ``kanban.sync.links_reader`` is ``replica``.
- :func:`links_diff` compares the two answers per chat; readers switch only once it is clean.
"""

from __future__ import annotations

from hermes_cli import kanban_lockguard as _kanban_lockguard
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

_log = logging.getLogger(__name__)

KINDS = ("created", "shepherd")
OPEN_STATUSES = ("todo", "ready", "running", "scheduled", "blocked", "review", "triage")


def link_uid(task_id: str, gateway: str, profile: Optional[str], session: str, kind: str) -> str:
    return f"{task_id}:{gateway}:{profile or '-'}:{session}:{kind}"


def _local_links(conn: sqlite3.Connection, gateway: str) -> dict[str, dict[str, Any]]:
    """Links from this board file's own data: creating chats and ``tui`` shepherd subscriptions."""
    from hermes_cli.kanban_session_activity import _counted_sub_clause
    now = int(time.time())
    out: dict[str, dict[str, Any]] = {}

    def add(task_id: str, session: str, kind: str, created_at: Any) -> None:
        uid = link_uid(task_id, gateway, None, session, kind)
        out[uid] = {"uid": uid, "task_id": task_id, "chat_gateway": gateway, "chat_profile": None,
                    "chat_session": session, "kind": kind, "released": 0, "created_at": int(created_at or now)}

    for task_id, session, created_at in conn.execute(
            "SELECT id, session_id, created_at FROM tasks WHERE session_id IS NOT NULL AND session_id != ''"):
        add(task_id, session, "created", created_at)
    try:
        for task_id, session in conn.execute(
                f"SELECT s.task_id, s.chat_id FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
                f"WHERE s.platform = 'tui' AND s.chat_id != '' AND {_counted_sub_clause(conn)}"):
            if link_uid(task_id, gateway, None, session, "created") not in out:
                add(task_id, session, "shepherd", None)
    except sqlite3.Error:  # board predates notify subscriptions
        pass
    return out


def _ledger_links(board: str, gateway: str) -> dict[str, dict[str, Any]]:
    """This gateway's chats' links to other gateways' cards on ``board``, from the origin-ledger projection."""
    from hermes_cli.kanban_session_activity import _remote_links
    now = int(time.time())
    out: dict[str, dict[str, Any]] = {}
    for card in _remote_links():
        if card.get("board") != board or card.get("origin_gateway") not in (None, gateway) or not card.get("session"):
            continue
        kind = "created" if card.get("via") == "origin" else "shepherd"
        profile = card.get("origin_profile")
        uid = link_uid(card["id"], gateway, profile, card["session"], kind)
        out[uid] = {"uid": uid, "task_id": card["id"], "chat_gateway": gateway, "chat_profile": profile,
                    "chat_session": card["session"], "kind": kind,
                    "released": 1 if card.get("released") else 0, "created_at": now}
    return out


def ledger_boards(gateway: str) -> set[str]:
    """Boards on which this gateway's chats have cards, per the origin-ledger projection."""
    from hermes_cli.kanban_session_activity import _remote_links
    return {c["board"] for c in _remote_links() if c.get("board") and c.get("origin_gateway") in (None, gateway)}


def reconcile(conn: sqlite3.Connection, board: str, gateway: str, *, ledger: bool = True) -> dict[str, int]:
    """Bring this gateway's links in ``conn`` (one board file) up to date. Writes go through the capture triggers,
    so every new or changed link is pushed to the service. Returns counts of added/changed/released links."""
    from hermes_cli.kanban_db_connect import write_txn
    want = _local_links(conn, gateway)
    if ledger:
        want.update(_ledger_links(board, gateway))
    have = {r[0]: r[1] for r in conn.execute(
        "SELECT uid, released FROM task_chat_links WHERE chat_gateway = ?", (gateway,))}
    added = changed = released = 0
    with write_txn(conn):
        for uid, link in want.items():
            if uid not in have:
                conn.execute(
                    "INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, released, "
                    "created_at) VALUES (:uid, :task_id, :chat_gateway, :chat_profile, :chat_session, :kind, :released, "
                    ":created_at)", link)
                added += 1
            elif have[uid] != link["released"]:
                conn.execute("UPDATE task_chat_links SET released = ? WHERE uid = ?", (link["released"], uid))
                changed += 1
        for uid, was_released in have.items():
            if uid not in want and not was_released:
                conn.execute("UPDATE task_chat_links SET released = 1 WHERE uid = ?", (uid,))
                released += 1
    return {"added": added, "changed": changed, "released": released}


def replica_cards(session_ids: Iterable[str], gateway: str, statuses: tuple[str, ...] = OPEN_STATUSES,
                  replicas: Optional[list[tuple[str, Path]]] = None) -> list[dict[str, Any]]:
    """This gateway's chats' (``session_ids``, one lineage) unreleased links to cards created on OTHER gateways,
    from every board's replica, shaped like the origin-ledger rows ``session_tasks`` appends."""
    ids = [s for s in dict.fromkeys(str(s) for s in session_ids if s)]
    if not ids:
        return []
    if replicas is None:
        from hermes_cli import kanban_sync_client as ksc
        replicas = [(slug, ksc.shadow_path(slug)) for slug, _path in ksc.board_paths()]
    from hermes_cli.kanban_session_activity import cached_ro  # lazy: that module imports this one
    ph = ",".join("?" for _ in ids)
    st = ",".join("?" for _ in statuses)
    now = time.time()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for board, path in replicas:
        if not path.exists():
            continue
        try:
            with cached_ro(path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT t.id, t.title, t.status, t.assignee, t.created_at, t.started_at, t.priority, t.block_kind, "
                    "t.human_request, t.created_on, l.chat_session, l.chat_profile, l.kind FROM task_chat_links l "
                    f"JOIN tasks t ON t.id = l.task_id WHERE l.chat_gateway = ? AND l.released = 0 AND l.chat_session IN ({ph}) "
                    f"AND t.status IN ({st}) AND t.created_on IS NOT NULL AND t.created_on != ?",
                    (gateway, *ids, *statuses, gateway)).fetchall()
        except sqlite3.Error as exc:
            _log.info("kanban chat links: replica %s unreadable (%s)", path, exc)
            continue
        for r in rows:
            owner = r["created_on"]
            key = f"{owner}/default/{board}/{r['id']}"
            pair = (r["chat_session"], key)  # one row per chat and card: a card often has several chats
            if pair in out:
                continue
            out[pair] = {
                "title": r["title"], "status": r["status"], "assignee": r["assignee"], "created_at": r["created_at"],
                "started_at": r["started_at"], "priority": r["priority"], "block_kind": r["block_kind"],
                "human_request": r["human_request"], "id": r["id"], "board": board, "gateway": owner, "key": key,
                "session": r["chat_session"], "session_id": r["chat_session"], "via": "origin" if r["kind"] == "created" else "shepherd",
                "remote": True, "profile": "default", "owner_profile": "default", "origin_gateway": gateway,
                "origin_profile": r["chat_profile"], "ownership_state": "qualified", "unfinished": True,
                "released": False, "reported_status": r["status"], "synced_at": now, "stale": False,
                "source": "replica",
            }
    return list(out.values())


def links_diff(gateway: str, statuses: tuple[str, ...] = OPEN_STATUSES) -> dict[str, Any]:
    """Per chat, the open remote cards the origin ledger lists against the ones the replicas list. Clean when the
    two answers match for every chat either side knows."""
    from hermes_cli.kanban_session_activity import _remote_links
    ledger: dict[str, set[tuple[str, str, str]]] = {}
    for card in _remote_links():
        if card.get("origin_gateway") not in (None, gateway) or not card.get("session"):
            continue
        if card.get("status") not in statuses or card.get("released"):
            continue
        ledger.setdefault(card["session"], set()).add((card["gateway"], card["board"], card["id"]))
    sessions = set(ledger)
    from hermes_cli import kanban_sync_client as ksc
    replicas = [(slug, ksc.shadow_path(slug)) for slug, _path in ksc.board_paths()]
    for _slug, path in replicas:
        if path.exists():
            with _kanban_lockguard.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2) as conn:
                sessions |= {r[0] for r in conn.execute(
                    "SELECT DISTINCT chat_session FROM task_chat_links WHERE chat_gateway = ? AND released = 0", (gateway,))}
    replica: dict[str, set[tuple[str, str, str]]] = {}
    for card in replica_cards(sessions, gateway, statuses, replicas):
        replica.setdefault(card["session"], set()).add((card["gateway"], card["board"], card["id"]))
    only_ledger = {s: sorted(ledger.get(s, set()) - replica.get(s, set())) for s in sessions}
    only_replica = {s: sorted(replica.get(s, set()) - ledger.get(s, set())) for s in sessions}
    only_ledger = {s: v for s, v in only_ledger.items() if v}
    only_replica = {s: v for s, v in only_replica.items() if v}
    return {"clean": not only_ledger and not only_replica, "chats": len(sessions),
            "ledger_links": sum(len(v) for v in ledger.values()), "replica_links": sum(len(v) for v in replica.values()),
            "only_in_ledger": only_ledger, "only_in_replica": only_replica}
