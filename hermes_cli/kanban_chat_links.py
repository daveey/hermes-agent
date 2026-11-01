"""Which chats a card belongs to, synced through the Board (RFC "Retiring the origin ledger", phase 5). [daveey]

``task_chat_links`` rows (card, chat gateway/profile/session, kind ``created``/``shepherd``, ``released``) travel
like any synced row, so every gateway's replica can answer "which cards does this chat have", on every gateway,
with one local query.

- :func:`reconcile` (run by the sync loop, idempotent, so its first run is the backfill) writes this gateway's
  links into a board file: chats that created a card here (``tasks.session_id``), chats that shepherd a card here
  (``tui`` subscriptions), and this gateway's chats' links to other gateways' cards from the origin-ledger
  projection, with its released state. Links it no longer finds are marked released, never deleted.
- :func:`replica_cards` answers a chat lineage's cards on other gateways from the replicas; ``session_tasks`` AND the
  sidebar counts use it instead of the origin ledger when ``kanban.sync.links_reader`` is ``replica``, so the
  count and the task strip are one projection in each mode.
- :func:`links_diff` compares the two answers per chat, ``unknown`` unfinished obligations included; readers
  switch only once it is clean.
"""

from __future__ import annotations

import json
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


def _worker_shepherd(session: Any, kind: str) -> bool:
    """A headless execution receipt is provenance, never a chat destination. [daveey]"""
    from hermes_cli.kanban_db_notify import is_headless_session
    return is_headless_session(session)


def _local_links(conn: sqlite3.Connection, gateway: str) -> dict[str, dict[str, Any]]:
    """Links from this board file's own data: creating chats and ``tui`` shepherd subscriptions."""
    from hermes_cli.kanban_session_activity import _counted_sub_clause
    now = int(time.time())
    out: dict[str, dict[str, Any]] = {}

    def add(task_id: str, session: str, kind: str, created_at: Any) -> None:
        from hermes_cli.kanban_placement import is_placeholder_session
        if is_placeholder_session(session) or _worker_shepherd(session, kind):
            return
        uid = link_uid(task_id, gateway, None, session, kind)
        out[uid] = {"uid": uid, "task_id": task_id, "chat_gateway": gateway, "chat_profile": None,
                    "chat_session": session, "kind": kind, "released": 0, "created_at": int(created_at or now)}

    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        for row in conn.execute(
                "SELECT l.* FROM task_chat_links l WHERE l.chat_gateway=? AND l.kind='created' "
                "AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id=l.task_id "
                "AND e.kind='creator_chats_resolved')", (gateway,)):
            if not _worker_shepherd(row["chat_session"], row["kind"]):
                out[row["uid"]] = dict(row)
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
        from hermes_cli.kanban_placement import is_placeholder_session
        kind = "created" if card.get("via") == "origin" else "shepherd"
        if is_placeholder_session(card["session"]) or _worker_shepherd(card["session"], kind):
            continue
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
    from hermes_cli.kanban_creator_chats import backfill
    backfill(conn)
    want = _local_links(conn, gateway)
    if ledger:
        want.update(_ledger_links(board, gateway))
    from hermes_cli.kanban_placement import is_placeholder_session
    rows = conn.execute("SELECT uid, released, chat_session, kind FROM task_chat_links WHERE chat_gateway = ?",
                        (gateway,)).fetchall()
    have = {r[0]: r[1] for r in rows}
    # A placement attribution id is not a chat: its link is deleted (captured, so the Board drops it too), never
    # merely released; a released one still counted in every diff and replica read. A worker transcript's
    # shepherd link is no chat either (its wakes are refused). [daveey]
    placeholders = {r[0] for r in rows if is_placeholder_session(r[2]) or _worker_shepherd(r[2], r[3])}
    added = changed = released = retired = 0
    with write_txn(conn):
        for uid in placeholders - set(want):
            conn.execute("DELETE FROM task_chat_links WHERE uid = ?", (uid,))
            have.pop(uid)
            retired += 1
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
    return {"added": added, "changed": changed, "released": released, "retired": retired}


TERMINAL_STATUSES = ("done", "archived")
UNKNOWN = "unknown"
# Rows last read from each replica, so an unreadable replica keeps its rows (stale) instead of dropping them.
_last_good: dict[tuple[str, str, str], tuple[list[dict[str, Any]], Optional[float]]] = {}


def _reset_for_tests() -> None:
    _last_good.clear()


def _stale_after_s() -> float:
    """A replica not pulled for this long is stale: the hub-sync window, or three sync periods if that is longer."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_session_activity import HUB_STALE_AFTER_S
    try:
        period = float(sync.sync_config().get("interval_seconds") or 0)
    except (TypeError, ValueError):
        period = 0.0
    return max(HUB_STALE_AFTER_S, 3 * period)


def _read_replica(path: Path, gateway: str, profile: str) -> tuple[list[dict[str, Any]], Optional[float]]:
    """Every unreleased link of this gateway+profile's chats in one replica, with its card row (or none when the
    card row is missing), and the replica's last successful pull. Raises ``sqlite3.Error`` when unreadable."""
    from hermes_cli.kanban_session_activity import cached_ro  # lazy: that module imports this one
    with cached_ro(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT l.task_id AS id, t.id AS task_row, t.title, t.status, t.assignee, t.created_at, t.started_at, "
            "t.priority, t.block_kind, t.human_request, t.created_on, l.chat_session, l.chat_profile, l.kind "
            "FROM task_chat_links l LEFT JOIN tasks t ON t.id = l.task_id "
            "WHERE l.chat_gateway = ? AND l.chat_profile = ? AND l.released = 0 "
            "AND (t.id IS NULL OR (t.created_on IS NOT NULL AND t.created_on != ?))",
            (gateway, profile, gateway)).fetchall()]
        try:
            row = conn.execute("SELECT pulled_at FROM kanban_sync_state LIMIT 1").fetchone()
            pulled = float(row[0]) if row and row[0] is not None else None
        except sqlite3.OperationalError:  # replica written before pulls were stamped
            pulled = None
    return rows, pulled


def _ledger_owners(stale_ok: bool = False) -> dict[tuple[str, str], list[tuple[str, str, str, bool]]]:
    """``(board, id) -> [(owner gateway, owner profile, session, released)]`` from the origin-ledger projection. A
    synced board is one file per gateway with one row per id, shared by that gateway's profiles, so the replica
    row carries no owner profile: the ledger only labels it (``_owner_of``). ``stale_ok`` is ``_remote_links``'s:
    take the last projection instead of waiting on a rebuild."""
    from hermes_cli.kanban_session_activity import _remote_links
    try:
        cards = _remote_links(stale_ok=True) if stale_ok else _remote_links()
    except Exception as exc:  # health: allow BLE001 -- the profile is a label; a failed ledger read must not hide cards
        _log.info("kanban chat links: owner profiles unavailable (%s: %s)", type(exc).__name__, exc)
        return {}
    out: dict[tuple[str, str], list[tuple[str, str, str, bool]]] = {}
    for c in cards:
        if c.get("gateway") and c.get("board") and c.get("id") and c.get("profile"):
            out.setdefault((str(c["board"]), str(c["id"])), []).append(
                (str(c["gateway"]), str(c["profile"]), str(c.get("session") or ""), bool(c.get("released"))))
    return out


def _owner_of(known: list[tuple[str, str, str, bool]], owner: Optional[str],
              session: str) -> tuple[Optional[str], str]:
    """The ONE (owner gateway, owner profile) for a replica card: the profile this chat's own unreleased ledger row
    names, else the only one any unreleased row names, else ``default`` (never several rows for one card)."""
    if owner is not None:
        known = [k for k in known if k[0] == owner]
    for pick in ([k for k in known if k[2] == session and not k[3]], [k for k in known if not k[3]]):
        pairs = sorted({(k[0], k[1]) for k in pick})
        if len(pairs) == 1:
            return pairs[0]
    gateways = sorted({k[0] for k in known})
    return (owner if owner is not None else gateways[0] if len(gateways) == 1 else None), "default"


def _ledger_fallback(board: str, gateway: str, profile: str, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    """Cold start with an unreadable replica: the ledger's rows for that board, all stale (never "no cards")."""
    from hermes_cli.kanban_session_activity import _remote_links
    try:
        cards = _remote_links()
    except Exception as exc:  # health: allow BLE001 -- both sources unreadable: nothing to show, logged
        _log.warning("kanban chat links: replica and ledger both unreadable for %s (%s)", board, exc)
        return []
    out = []
    for c in cards:
        if (c.get("board") != board or c.get("origin_gateway") not in (None, gateway)
                or c.get("origin_profile") not in (None, profile) or not c.get("session") or c.get("released")):
            continue
        if c.get("status") in statuses or (c.get("status") == UNKNOWN and c.get("unfinished")):
            out.append({**c, "session_id": c["session"], "remote": True, "stale": True, "source": "ledger-fallback"})
    return out


def replica_cards(session_ids: Optional[Iterable[str]], gateway: str, statuses: tuple[str, ...] = OPEN_STATUSES,
                  replicas: Optional[list[tuple[str, Path]]] = None, *, now: Optional[float] = None,
                  ledger_fallback: bool = True, unreadable_out: Optional[list[str]] = None,
                  stale_ok: bool = False) -> list[dict[str, Any]]:
    """This gateway's chats' (``session_ids``, one lineage; ``None`` = every chat) unreleased links to cards created
    on OTHER gateways, from every board's replica, shaped like the origin-ledger rows ``session_tasks`` appends.

    The sidebar counts read this too when ``links_reader`` is ``replica``, so both answers are one projection:
    - an unreleased link to a ``done``/``archived`` card, or to a card row the replica lacks, is an ``unknown``,
      unfinished obligation (the ledger's done-without-verified-release); only a released link drops it;
    - only this origin profile's links (``chat_profile``) count;
    - ``stale``/``synced_at`` come from the replica's last successful pull;
    - an unreadable replica keeps its last rows, stale; on a cold start the ledger's rows for that board, stale.
      ``ledger_fallback=False`` (links_diff) skips both, so a diff never compares the ledger with itself;
      ``unreadable_out`` collects the boards whose replica could not be read;
    - one row per (chat, card); the key carries the owner profile the ledger names for it, else ``default``;
    - ``stale_ok`` (the Desktop linked-card strip): the owner labels come from the last ledger projection rather
      than waiting behind a rebuild of it. [daveey] Without it the strip's ``stale_ok`` stopped at this call and
      ``/api/session-kanban-tasks`` queued on ``_links_lock`` for every board write (3-7 s in a wake burst)."""
    from hermes_cli.profiles import get_active_profile_name
    ids = None if session_ids is None else set(str(s) for s in session_ids if s)
    if ids is not None and not ids:
        return []
    if replicas is None:
        from hermes_cli import kanban_sync_client as ksc
        replicas = [(slug, ksc.shadow_path(slug)) for slug, _path in ksc.board_paths()]
    profile = get_active_profile_name()
    now = time.time() if now is None else now
    stale_after = _stale_after_s()
    owners: Optional[dict[tuple[str, str], list[tuple[str, str, str, bool]]]] = None
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for board, path in replicas:
        memo_key = (str(path), gateway, profile)
        try:
            rows, pulled = _read_replica(path, gateway, profile)
            _last_good[memo_key] = (rows, pulled)
            stale = pulled is None or now - pulled > stale_after
        except sqlite3.Error as exc:
            if unreadable_out is not None:
                unreadable_out.append(board)
            if not ledger_fallback:
                continue
            hit = _last_good.get(memo_key)
            _log.info("kanban chat links: replica %s unreadable (%s); %s", path, exc,
                      "keeping its last rows as stale" if hit else "falling back to the ledger, stale")
            if hit is None:
                for c in _ledger_fallback(board, gateway, profile, statuses):
                    if ids is None or c["session"] in ids:
                        out.setdefault((c["session"], c["key"]), c)
                continue
            rows, pulled = hit
            stale = True
        if owners is None and rows:
            owners = _ledger_owners(stale_ok)
        for r in rows:
            if ids is not None and r["chat_session"] not in ids:
                continue
            status = r["status"]
            missing = r["task_row"] is None
            unknown = missing or status in TERMINAL_STATUSES or status is None
            if not unknown and status not in statuses:
                continue
            tid = str(r["id"])
            owner, owner_profile = _owner_of((owners or {}).get((board, tid), []), r["created_on"], r["chat_session"])
            if missing and owner == gateway:  # the ledger places the missing row on this gateway: a local card
                continue
            key = f"{owner or '?'}/{owner_profile}/{board}/{tid}"
            pair = (r["chat_session"], key)  # one row per chat and card: a card often has several chats
            if pair in out:
                continue
            out[pair] = {
                "title": r["title"], "status": UNKNOWN if unknown else status, "assignee": r["assignee"],
                "created_at": r["created_at"], "started_at": r["started_at"], "priority": r["priority"],
                "block_kind": r["block_kind"], "human_request": _decoded_request(r["human_request"]), "id": tid,
                "board": board,
                "gateway": owner, "key": key, "session": r["chat_session"], "session_id": r["chat_session"],
                "via": "origin" if r["kind"] == "created" else "shepherd", "remote": True,
                "profile": owner_profile, "owner_profile": owner_profile, "origin_gateway": gateway,
                "origin_profile": r["chat_profile"],
                "ownership_state": "qualified" if owner is not None else "unknown", "unfinished": True,
                "released": False, "reported_status": status, "synced_at": pulled, "stale": stale,
                "source": "replica",
            }
    return list(out.values())


def _decoded_request(value: Any) -> Any:
    """[daveey] The replica stores ``human_request`` as JSON text; the ledger rows (and ``needs_human``) carry the dict.
    Undecoded, a card blocked on the user read as open work and its chat painted waiting instead of red. Malformed
    text is no evidence (None), the same verdict ``needs_human`` gives it."""
    if isinstance(value, (bytes, str)):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _ledger_obligation(card: dict[str, Any], statuses: tuple[str, ...]) -> bool:
    """An unreleased ledger row the sidebar counts: open, or ``unknown`` and unfinished (done, release unverified)."""
    if card.get("released"):
        return False
    return card.get("status") in statuses or (card.get("status") == UNKNOWN and bool(card.get("unfinished")))


def links_diff(gateway: str, statuses: tuple[str, ...] = OPEN_STATUSES) -> dict[str, Any]:
    """Per chat, the remote obligations the origin ledger lists against the ones the replicas list, including
    ``unknown`` unfinished rows (done cards without a verified release). Clean when the two answers match for
    every chat either side knows."""
    from hermes_cli.kanban_session_activity import _remote_links
    ledger: dict[str, set[tuple[str, str, str]]] = {}
    for card in _remote_links():
        if card.get("origin_gateway") not in (None, gateway) or not card.get("session"):
            continue
        if not _ledger_obligation(card, statuses):
            continue
        ledger.setdefault(card["session"], set()).add((card["gateway"], card["board"], card["id"]))
    replica: dict[str, set[tuple[str, str, str]]] = {}
    unreadable: list[str] = []
    from hermes_cli import kanban_sync_client as ksc
    replicas = [(slug, ksc.shadow_path(slug)) for slug, _path in ksc.board_paths()]
    for card in replica_cards(None, gateway, statuses, replicas, ledger_fallback=False, unreadable_out=unreadable):
        replica.setdefault(card["session"], set()).add((card["gateway"], card["board"], card["id"]))
    # A replica that exists but cannot be read, or that is missing while the ledger has cards on its board, answers
    # nothing: the diff is never clean while one does (readers must not switch on it). A missing replica of a board
    # with no ledger cards has nothing to compare.
    exists = {slug for slug, path in replicas if path.exists()}
    ledger_boards = {b for cards in ledger.values() for _gw, b, _tid in cards}
    unreadable = sorted({b for b in unreadable if b in exists or b in ledger_boards})
    sessions = set(ledger) | set(replica)
    only_ledger = {s: sorted(ledger.get(s, set()) - replica.get(s, set())) for s in sessions}
    only_replica = {s: sorted(replica.get(s, set()) - ledger.get(s, set())) for s in sessions}
    only_ledger = {s: v for s, v in only_ledger.items() if v}
    only_replica = {s: v for s, v in only_replica.items() if v}
    # An unreadable replica answers nothing (see above).
    return {"clean": not only_ledger and not only_replica and not unreadable, "chats": len(sessions),
            "ledger_links": sum(len(v) for v in ledger.values()), "replica_links": sum(len(v) for v in replica.values()),
            "only_in_ledger": only_ledger, "only_in_replica": only_replica, "unreadable": unreadable}
