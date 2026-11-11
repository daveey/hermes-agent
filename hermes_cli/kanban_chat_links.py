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

from hermes_cli.kanban_run_machine import placement_select, run_machine

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
    """Links from this board file's own data: creating chats and ``tui`` shepherd subscriptions.

    A machine session of this gateway (``is_machine_session``: a worker/cron/one-shot/subagent transcript) is no
    chat, so it is never wanted: :func:`reconcile` releases a link to it and never writes one back. [daveey]"""
    from hermes_cli.kanban_db_notify import machine_sessions
    from hermes_cli.kanban_session_activity import _counted_sub_clause
    now = int(time.time())
    out: dict[str, dict[str, Any]] = {}
    has_links = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone())
    created = [dict(r) for r in conn.execute(
        "SELECT l.* FROM task_chat_links l WHERE l.chat_gateway=? AND l.kind='created' "
        "AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id=l.task_id "
        "AND e.kind='creator_chats_resolved')", (gateway,))] if has_links else []
    # Cards this gateway owns (a picked-up foreign copy keeps the owner's answer, see _owner_written_releases).
    foreign = {r[0] for r in conn.execute(
        "SELECT id FROM tasks WHERE created_on IS NOT NULL AND created_on != '' AND created_on != ?", (gateway,))} \
        if created and any(c[1] == "created_on" for c in conn.execute("PRAGMA table_info(tasks)")) else set()
    sessions = [tuple(r) for r in conn.execute(
        "SELECT id, session_id, created_at FROM tasks WHERE session_id IS NOT NULL AND session_id != ''")]
    try:
        shepherds = [tuple(r) for r in conn.execute(
            f"SELECT s.task_id, s.chat_id FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
            f"WHERE s.platform = 'tui' AND s.chat_id != '' AND {_counted_sub_clause(conn)}")]
    except sqlite3.Error:  # board predates notify subscriptions
        shepherds = []
    machine = machine_sessions(gateway, [r["chat_session"] for r in created] + [r[1] for r in sessions]
                               + [r[1] for r in shepherds], local_gateway=gateway)

    def add(task_id: str, session: str, kind: str, created_at: Any) -> None:
        from hermes_cli.kanban_placement import is_placeholder_session
        if is_placeholder_session(session) or _worker_shepherd(session, kind) or session in machine:
            return
        uid = link_uid(task_id, gateway, None, session, kind)
        out[uid] = {"uid": uid, "task_id": task_id, "chat_gateway": gateway, "chat_profile": None,
                    "chat_session": session, "kind": kind, "released": 0, "created_at": int(created_at or now)}

    for row in created:
        if not _worker_shepherd(row["chat_session"], row["kind"]) and row["chat_session"] not in machine:
            # [daveey] Wanted unreleased, like a ``tasks.session_id`` creator link: creator-chat ownership has no
            # release on this gateway but the machine/placeholder rules above. Keeping the stored flag let one
            # foreign reconcile (a named-profile gateway on an older core holding the host sync lock) release it
            # for good, and the shepherd link this link replaces was then released too: the chat lost an open
            # card it handed work to (hub chat 20261016_123957_6212f7 -> t_01a19084a3b36956, 2026-11-02).
            out[row["uid"]] = row if row["task_id"] in foreign else {**row, "released": 0}
    for task_id, session, created_at in sessions:
        add(task_id, session, "created", created_at)
    for task_id, session in shepherds:
        if link_uid(task_id, gateway, None, session, "created") not in out:
            add(task_id, session, "shepherd", None)
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
        from hermes_cli.kanban_db_notify import is_machine_session
        if is_machine_session(gateway, card["session"], local_gateway=gateway):
            continue
        profile = card.get("origin_profile")
        uid = link_uid(card["id"], gateway, profile, card["session"], kind)
        out[uid] = {"uid": uid, "task_id": card["id"], "chat_gateway": gateway, "chat_profile": profile,
                    "chat_session": card["session"], "kind": kind,
                    "released": 1 if card.get("released") else 0, "created_at": now}
    return out


def _owner_written_releases(board: str, gateway: str, want: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Released copies of the ``created`` links another gateway (the card's owner) wrote for this gateway's chats,
    once the replica shows the card closed (``done``/``archived``). [daveey]

    The owner writes them in ``kanban_creator_chats`` (``initialize_ownership`` / ``inherit_chats``) for a remote
    origin chat. The owner's reconcile manages only its own chats' links, so nothing released them: a done card
    whose origin ledger had no row for that chat (no ledger link with the same uid, no released shepherd sibling)
    stayed an ``unknown`` obligation in ``replica_cards`` forever (4 hub cards on macbook chat
    20261027_134353_5ab65c, 2026-11-01). The chat's gateway releases them by writing the same uid (the same row on
    the Board) with ``released = 1``, as it already does for a ledger ``origin`` row. Open cards and missing card
    rows are left alone; links ``want`` already carries keep the ledger's answer. A card reopened after the release
    stays released for that chat: the uid is one row, so the owner's original intent is not recoverable here.

    A link to a machine session of this gateway (``is_machine_session``) is released whatever the card's status:
    no human reads that session, so it owns nothing even on an open card (hub t_01a19864fc42f5b2 was owned by
    MacBook worker transcript 20261029_180356_84db5f, 2026-11-01)."""
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli.kanban_db_notify import machine_sessions
    from hermes_cli.kanban_lockguard import connect
    from hermes_cli.kanban_placement import is_placeholder_session
    path = ksc.shadow_path(board)
    if not path.exists():
        return {}
    try:
        conn = connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(
                "SELECT l.uid, l.task_id, l.chat_gateway, l.chat_profile, l.chat_session, l.kind, l.created_at, "
                "t.status FROM task_chat_links l JOIN tasks t ON t.id = l.task_id "
                "WHERE l.chat_gateway = ? AND l.kind = 'created' AND l.released = 0 AND l.uid IS NOT NULL "
                "AND t.created_on IS NOT NULL AND t.created_on != ?",
                (gateway, gateway))]
        finally:
            conn.close()
    except sqlite3.Error as exc:  # an unreadable replica releases nothing this round; the next one retries
        _log.info("kanban chat links: replica %s unreadable for owner-link releases (%s)", path, exc)
        return {}
    machine = machine_sessions(gateway, [r["chat_session"] for r in rows], local_gateway=gateway)
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["uid"] in want or is_placeholder_session(r["chat_session"]) or _worker_shepherd(r["chat_session"], "created"):
            continue
        if r.pop("status") not in TERMINAL_STATUSES and r["chat_session"] not in machine:
            continue
        out[r["uid"]] = {**r, "released": 1, "created_at": int(r["created_at"] or time.time())}
    return out


def ledger_boards(gateway: str) -> set[str]:
    """Boards on which this gateway's chats have cards, per the origin-ledger projection."""
    from hermes_cli.kanban_session_activity import _remote_links
    return {c["board"] for c in _remote_links() if c.get("board") and c.get("origin_gateway") in (None, gateway)}


def _plan(conn: sqlite3.Connection, board: str, gateway: str, ledger: bool) -> tuple[dict, list]:
    """``(want, rows)``: the links this gateway wants in ``conn`` and the rows it has (read-only)."""
    want = _local_links(conn, gateway)
    if ledger:
        want.update(_ledger_links(board, gateway))
    want.update(_owner_written_releases(board, gateway, want))
    rows = conn.execute("SELECT uid, released, chat_session, kind, task_id FROM task_chat_links "
                        "WHERE chat_gateway = ?", (gateway,)).fetchall()
    return want, rows


def _machine_releases(want: dict, rows: list, gateway: str) -> list[dict[str, Any]]:
    """The links a reconcile pass releases because their chat is a machine session of this gateway: an unreleased
    row nothing wants any more, or an owner-written row it writes back released (``_owner_written_releases``)."""
    from hermes_cli.kanban_db_notify import is_headless_session, machine_sessions
    from hermes_cli.kanban_placement import is_placeholder_session
    have = {r[0]: r for r in rows}
    out = [{"uid": r[0], "task_id": r[4], "chat_session": r[2], "kind": r[3]} for r in rows
           if not r[1] and r[0] not in want]
    out += [{"uid": uid, "task_id": l["task_id"], "chat_session": l["chat_session"], "kind": l["kind"]}
            for uid, l in want.items() if l["released"] and (uid not in have or not have[uid][1])]
    out = [o for o in out if not is_placeholder_session(o["chat_session"]) and not is_headless_session(o["chat_session"])]
    machine = machine_sessions(gateway, [o["chat_session"] for o in out], local_gateway=gateway)
    return sorted((o for o in out if o["chat_session"] in machine), key=lambda o: o["uid"])


def machine_links(conn: sqlite3.Connection, board: str, gateway: str, *, ledger: bool = True) -> list[dict[str, Any]]:
    """Dry run: the links to this gateway's machine sessions the next :func:`reconcile` of ``board`` releases."""
    return _machine_releases(*_plan(conn, board, gateway, ledger), gateway)


def _write_release_receipt(board: str, gateway: str, released: list[dict[str, Any]]) -> None:
    """``~/.hermes/kanban/repairs/machine-links-<board>-<ts>.json``: the uids one pass released (audit trail)."""
    from hermes_cli import kanban_db
    try:
        path = kanban_db.kanban_home() / "kanban" / "repairs" / (
            f"machine-links-{board}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{len(released)}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"board": board, "gateway": gateway, "released_at": int(time.time()),
                                    "released": released}, indent=1) + "\n", encoding="utf-8")
        _log.info("kanban chat links: %s released %d links to machine sessions (receipt %s)", board, len(released), path)
    except OSError as exc:  # the release itself is committed; only the audit copy is lost
        _log.warning("kanban chat links: machine-link receipt for %s not written (%s)", board, exc)


def reconcile(conn: sqlite3.Connection, board: str, gateway: str, *, ledger: bool = True) -> dict[str, int]:
    """Bring this gateway's links in ``conn`` (one board file) up to date. Writes go through the capture triggers,
    so every new or changed link is pushed to the service. Returns counts of added/changed/released links.

    A link to a machine session of this gateway (``kanban_db_notify.is_machine_session``) is released here, through
    the same captured writes; each pass that releases any writes a receipt (``_write_release_receipt``). [daveey]"""
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_creator_chats import backfill
    backfill(conn)
    want, rows = _plan(conn, board, gateway, ledger)
    machine = _machine_releases(want, rows, gateway)
    from hermes_cli.kanban_placement import is_placeholder_session
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
    if machine:
        _write_release_receipt(board, gateway, machine)
    return {"added": added, "changed": changed, "released": released, "retired": retired}


TERMINAL_STATUSES = ("done", "archived")
UNKNOWN = "unknown"
# Rows last read from each replica, so an unreadable replica keeps its rows (stale) instead of dropping them.
_last_good: dict[tuple[str, str, str], tuple[list[dict[str, Any]], Optional[float]]] = {}


def _reset_for_tests() -> None:
    global _owners_memo
    _last_good.clear()
    _replica_rows.clear()
    _owners_memo = None


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
    card row is missing), and the replica's last successful pull. Raises ``sqlite3.Error`` when unreadable.

    ``chat_released`` is 1 when another link of the same card and chat (other kind) is released. [daveey] The owner
    writes a ``created`` link for a remote origin chat (``kanban_creator_chats``) that no gateway ever releases: the
    owner's reconcile only manages its own chats' links and the origin's only its own board file. The origin's
    ``shepherd`` link carries the verified release instead, so a done card stayed an ``unknown`` obligation forever
    (sidebar "4 open" naming none, 2026-11-01)."""
    from hermes_cli.kanban_activity_revision import SHADOW_TABLES, is_revision
    from hermes_cli.kanban_session_activity import _board_content_key, cached_ro  # lazy: that module imports this one
    # [daveey] The link rows only change with the replica's tasks/task_chat_links content; a pull that only moves
    # ``pulled_at`` (every few seconds) re-reads the stamp, not the ~10k links. Rows are read-only to callers.
    memo_key = (str(path), gateway, profile)
    content = _board_content_key(path, SHADOW_TABLES)
    hit = _replica_rows.get(memo_key)
    with cached_ro(path) as conn:
        conn.row_factory = sqlite3.Row
        # Only a trigger-backed revision vouches for the rows; a stat key (no counter) always reads.
        rows = hit[1] if hit is not None and hit[0] == content and is_revision(content) else None
        if rows is None:
            rows = _read_replica_links(conn, gateway, profile)
            _replica_rows[memo_key] = (content, rows)
        try:
            row = conn.execute("SELECT pulled_at FROM kanban_sync_state LIMIT 1").fetchone()
            pulled = float(row[0]) if row and row[0] is not None else None
        except sqlite3.OperationalError:  # replica written before pulls were stamped
            pulled = None
    return rows, pulled


# (replica path, gateway, profile) -> (content key, link rows) of the last ``_read_replica``.
_replica_rows: dict[tuple[str, str, str], tuple[tuple, list[dict[str, Any]]]] = {}


def _read_replica_links(conn: sqlite3.Connection, gateway: str, profile: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT l.task_id AS id, t.id AS task_row, t.title, t.status, t.assignee, t.created_at, t.started_at, "
        f"t.priority, t.block_kind, t.human_request, t.created_on, {placement_select(conn, 't')}, "
        "l.chat_session, l.chat_profile, l.kind, "
        "EXISTS (SELECT 1 FROM task_chat_links r WHERE r.task_id = l.task_id AND r.chat_gateway = l.chat_gateway "
        "AND r.chat_profile IS l.chat_profile AND r.chat_session = l.chat_session AND r.released = 1) "
        "AS chat_released "
        "FROM task_chat_links l LEFT JOIN tasks t ON t.id = l.task_id "
        "WHERE l.chat_gateway = ? AND l.chat_profile = ? AND l.released = 0 "
        "AND (t.id IS NULL OR (t.created_on IS NOT NULL AND t.created_on != ?))",
        (gateway, profile, gateway)).fetchall()]


def _ledger_owners(stale_ok: bool = False) -> dict[tuple[str, str], list[tuple[str, str, str, bool]]]:
    """``(board, id) -> [(owner gateway, owner profile, session, released)]`` from the origin-ledger projection. A
    synced board is one file per gateway with one row per id, shared by that gateway's profiles, so the replica
    row carries no owner profile: the ledger only labels it (``_owner_of``). ``stale_ok`` is ``_remote_links``'s:
    take the last projection instead of waiting on a rebuild."""
    from hermes_cli.kanban_session_activity import _remote_links
    global _owners_memo
    try:
        cards = _remote_links(stale_ok=True) if stale_ok else _remote_links()
    except Exception as exc:  # health: allow BLE001 -- the profile is a label; a failed ledger read must not hide cards
        _log.info("kanban chat links: owner profiles unavailable (%s: %s)", type(exc).__name__, exc)
        return {}
    # [daveey] ``_remote_links`` hands out a new list of the SAME read-only card dicts until it re-projects: the same
    # cards (by identity; the memo holds them, so no id is reused) give the same owners. Rebuilding this index from
    # ~5k cards was ~15 ms of every count recompute on David-MBP.
    memo = _owners_memo
    if memo is not None and len(memo[0]) == len(cards) and all(a is b for a, b in zip(memo[0], cards)):
        return memo[1]
    out: dict[tuple[str, str], list[tuple[str, str, str, bool]]] = {}
    for c in cards:
        if c.get("gateway") and c.get("board") and c.get("id") and c.get("profile"):
            out.setdefault((str(c["board"]), str(c["id"])), []).append(
                (str(c["gateway"]), str(c["profile"]), str(c.get("session") or ""), bool(c.get("released"))))
    _owners_memo = (cards, out)
    return out


# (cards, owners) of the last ``_ledger_owners`` build. Callers treat the owners map as read-only.
_owners_memo: tuple[list[dict[str, Any]], dict[tuple[str, str], list[tuple[str, str, str, bool]]]] | None = None


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
            # A closed card this chat's other link released is settled for this chat (see ``_read_replica``). Only a
            # terminal row: an open card keeps every unreleased link, and a missing row stays unknown.
            if status in TERMINAL_STATUSES and r.get("chat_released"):
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
            if machine := run_machine(r):
                out[pair]["run_machine"] = machine
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
