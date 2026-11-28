"""Resolve worker provenance into durable human chat ownership. [daveey]"""
from __future__ import annotations

import json
import sqlite3

from hermes_cli.kanban_links_chat_key import degrade as _degrade  # swallowed read errors leave no links key

MAX_DEPTH = 16


def initialize_ownership(conn, task_id, creator_task_id, parents, created_at, chat_origin, native_origin=None):
    """Resolve chat ownership, then record the machine-local native origin receipt (same transaction)."""
    _resolve_ownership(conn, task_id, creator_task_id, parents, created_at, chat_origin)
    from hermes_cli.kanban_origin_native import record_creation
    record_creation(conn, task_id, native_origin, created_at)


def _resolve_ownership(conn, task_id, creator_task_id, parents, created_at, chat_origin):
    """An explicit validated origin wins over ambient worker/ancestor ownership.

    It replaces only the CREATOR's ownership (link, origin sub, ambient session). Dependency parents' owning subs
    are still inherited (the ACK edge: a parent's chat hears this child block or finish). A headless origin (a
    worker/cron receipt) owns nothing, so it falls through to normal creator resolution, and a card nothing
    resolves gets ``creator_chats_unresolved`` so the census and backfill can find it. [daveey]"""
    from hermes_cli.kanban_db_graph import inherit_creator_origin
    from hermes_cli.kanban_db_notify import _inherit_notify_subs, is_headless_session
    from hermes_cli.kanban_db import _append_event
    from hermes_cli.kanban_origin import parse_origin
    from hermes_cli.kanban_chat_links import link_uid
    from hermes_cli.kanban_session_provenance import proven_foreign_chats

    origin = parse_origin(chat_origin) if chat_origin else None
    # The ancestor walk reads this card's ``created`` event and task_links: create_task writes both first.
    if origin is None or (is_headless_session(origin.chat_id)
                          and origin.chat_id not in proven_foreign_chats(conn, (task_id,))):
        inherit_creator_origin(conn, task_id, creator_task_id, created_at=created_at)
        _inherit_notify_subs(conn, task_id, parents, created_at=created_at)
        if origin is not None and not conn.execute(
                "SELECT 1 FROM task_events WHERE task_id=? AND kind IN "
                "('creator_chats_resolved','creator_chats_unresolved')", (task_id,)).fetchone():
            _append_event(conn, task_id, "creator_chats_unresolved",
                          {"task_id": task_id, "creator_task_id": creator_task_id,
                           "headless_origin": origin.ref, "max_depth": MAX_DEPTH})
        return
    conn.execute("UPDATE tasks SET session_id=NULL WHERE id=?", (task_id,))
    _append_event(conn, task_id, "creator_chats_resolved", {"explicit_origin": origin.ref})
    _inherit_notify_subs(conn, task_id, parents, created_at=created_at)
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.profiles import get_active_profile_name
    local = origin.gateway == (gateway_names()[0] or "local") and origin.profile == get_active_profile_name()
    platform, chat_id = ("tui", origin.session) if local else ("hub", origin.chat_id)
    conn.execute(
        "INSERT OR IGNORE INTO kanban_notify_subs "
        "(task_id,platform,chat_id,thread_id,delivery_mode,delivery_metadata,created_at,last_event_id) "
        "VALUES (?,?,?,'','notify',?, ?, (SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?))",
        (task_id, platform, chat_id, json.dumps({"reason": "origin"}), created_at, task_id))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        conn.execute(
            "INSERT OR IGNORE INTO task_chat_links "
            "(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
            "VALUES (?,?,?,?,?,'created',0,?)",
            (link_uid(task_id, origin.gateway, origin.profile, origin.session, "created"), task_id,
             origin.gateway, origin.profile, origin.session, created_at))


def _chat_links(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    """Include legacy courier origins that have a subscription but no projected link yet."""
    from hermes_cli.kanban_db_notify import _owning_sub_clause
    from hermes_cli.kanban_origin import parse_origin, OriginError

    rows = [dict(r) for r in conn.execute("SELECT * FROM task_chat_links WHERE task_id=?", (task_id,))]
    known = {(r["chat_gateway"], r["chat_profile"] or "default", r["chat_session"]) for r in rows}
    out = [r for r in rows if not r["released"]]
    for sub in conn.execute(
            "SELECT s.* FROM kanban_notify_subs s LEFT JOIN tasks p ON p.id=s.task_id "
            f"WHERE s.task_id=? AND s.platform='hub' AND {_owning_sub_clause()}", (task_id,)):
        try:
            origin = parse_origin(sub["chat_id"])
        except OriginError:
            continue
        key = (origin.gateway, origin.profile, origin.session)
        if key not in known:
            out.append({"task_id": task_id, "chat_gateway": origin.gateway, "chat_profile": origin.profile,
                        "chat_session": origin.session, "created_at": sub["created_at"]})
            known.add(key)
    return out


def resolve_chats(conn: sqlite3.Connection, creator_task_id: str, *, verdicts: LinkVerdicts | None = None
                  ) -> list[dict]:
    """Nearest unreleased human links; creator edges precede dependency parents, cycles stop.

    A machine session (``is_machine_session``: a worker/cron/one-shot/subagent transcript, whatever its id
    shape) owns nothing: its link is skipped and the walk goes on to the next level. Another gateway's
    ``local-<sha256>`` chat is one only without this board's proof on the card or an ancestor
    (``proven_foreign_chats``). With ``verdicts`` (read before the caller's write lock) the machine test is a
    lookup and never reads state.db. [daveey]"""
    from hermes_cli.kanban_db_notify import is_machine_session
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.kanban_placement import is_placeholder_session
    from hermes_cli.kanban_session_provenance import proven_foreign_chats

    me = gateway_names()[0]
    proofs: dict[str, frozenset[str]] = {}

    def _owner(task_id: str, row: dict) -> bool:
        if verdicts is not None:
            machine = verdicts.is_machine(row["chat_gateway"], row["chat_profile"], row["chat_session"])
        else:
            machine = is_machine_session(row["chat_gateway"], row["chat_session"], local_gateway=me,
                                         profile=row["chat_profile"])
        if not machine:
            return True
        if task_id not in proofs:
            proofs[task_id] = proven_foreign_chats(conn, (task_id,), local_gateway=me)
        profile = row['chat_profile'] if row['chat_profile'] is not None else 'default'
        return f"{row['chat_gateway']}/{profile}/{row['chat_session']}" in proofs[task_id]

    from hermes_cli.kanban_owner_chat import PREFIX
    frontier, seen, fallback = [creator_task_id], set(), []
    for _depth in range(MAX_DEPTH):
        owners, following = {}, []
        for task_id in frontier:
            if task_id in seen:
                continue
            seen.add(task_id)
            for row in _chat_links(conn, task_id):
                # Passive visibility is not ownership. Preserve source provenance before
                # inherit_chats records the selected owner as the child's created link.
                if row.get("kind", "created") != "created":
                    continue
                if not is_placeholder_session(row["chat_session"]) and _owner(task_id, row):
                    key = (row["chat_gateway"], row["chat_profile"], row["chat_session"])
                    owners.setdefault(key, dict(row))
            row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='created' "
                               "ORDER BY id LIMIT 1", (task_id,)).fetchone()
            payload = json.loads(row[0] or "{}") if row else {}
            if payload.get("creator_task_id"):
                following.append(payload["creator_task_id"])
            following.extend(r[0] for r in conn.execute(
                "SELECT parent_id FROM task_links WHERE child_id=? ORDER BY parent_id", (task_id,)))
        # A producer's standing owner chat (kanban_owner_chat) is the last resort: the walk goes on for a real
        # chat further up, and only the nearest owner chats are returned when there is none.
        real = [v for k, v in owners.items() if not str(k[2] or "").startswith(PREFIX)]
        if real:
            return real
        fallback = fallback or list(owners.values())
        frontier = list(dict.fromkeys(t for t in following if t not in seen))
        if not frontier:
            break
    return fallback


def _copy_delivery(conn: sqlite3.Connection, task_id: str, owner: dict) -> None:
    """Retain every delivery column, never the source card's notification cursor.

    A picked-up or synced copy of another gateway's owner card (``created_on`` set and not this gateway) carries
    its links but never its subs (they do not sync): with nothing to copy, the link alone still owns the child, as
    on the replica path. This gateway's own card is authoritative: a chat that removed its sub there stays
    unsubscribed, so its children get no sub either. [daveey]"""
    hub_chat = f"{owner['chat_gateway']}/{owner['chat_profile'] or 'default'}/{owner['chat_session']}"
    match = "((platform='hub' AND chat_id=?) OR (platform='tui' AND chat_id=?))"
    conn.execute(
        "INSERT OR IGNORE INTO kanban_notify_subs "
        "(task_id, platform, chat_id, thread_id, user_id, user_id_alt, chat_type, notifier_profile, "
        "delivery_mode, delivery_metadata, created_at, last_event_id) "
        "SELECT ?, platform, chat_id, thread_id, user_id, user_id_alt, chat_type, notifier_profile, "
        "delivery_mode, delivery_metadata, unixepoch(), "
        "(SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?) "
        "FROM kanban_notify_subs WHERE task_id=? AND " + match,
        (task_id, task_id, owner["task_id"], hub_chat, owner["chat_session"]))
    if _foreign_copy(conn, owner["task_id"]) and not conn.execute(
            f"SELECT 1 FROM kanban_notify_subs WHERE task_id=? AND {match}",
            (task_id, hub_chat, owner["chat_session"])).fetchone():
        _owner_delivery(conn, task_id, owner)


def _foreign_copy(conn: sqlite3.Connection, task_id: str) -> bool:
    """True for a local copy of another gateway's card (its subs live on that gateway, not here). [daveey]"""
    from hermes_cli.kanban_origin import gateway_names
    if not any(c[1] == "created_on" for c in conn.execute("PRAGMA table_info(tasks)")):
        return False
    row = conn.execute("SELECT created_on FROM tasks WHERE id=?", (task_id,)).fetchone()
    return bool(row and row[0] and row[0] != gateway_names()[0])


class _ReplicaReader:
    """One lazy guarded handle per repair pass, never cached ownership or misses.

    Backfill must retry even terminal cards: a creator's human link can arrive late.
    Reusing setup, rather than skipping candidates, bounds opens without hiding those arrivals.
    The next pass always opens anew (including after a missing or replaced replica).
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.replica = None
        self.attempted = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.replica is not None:
            self.replica.close()

    def _open(self):
        from pathlib import Path
        from hermes_cli import kanban_db_sync as sync
        from hermes_cli.kanban_sync_client import shadow_path
        from hermes_cli import kanban_lockguard

        key = sync._db_key(self.conn)
        if not key or Path(key).parent.name in (sync.SHADOW_DIR, sync.LINKS_DIR):
            return
        path = shadow_path(sync.board_slug_for_path(key))
        if not path.exists() or path.resolve() == Path(key).resolve():
            return
        replica = kanban_lockguard.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            replica.row_factory = sqlite3.Row
            if replica.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
                self.replica = replica
        finally:
            if self.replica is None:
                replica.close()

    def open(self):
        """The guarded replica handle (opened at most once per pass), or None when there is none or it fails."""
        try:
            if not self.attempted:
                self.attempted = True
                self._open()
        except (OSError, sqlite3.Error):
            _degrade()
            return None
        return self.replica

    def resolve(self, creator_task_id: str, *, verdicts: LinkVerdicts | None = None) -> list[dict]:
        try:
            replica = self.open()
            return resolve_chats(replica, creator_task_id, verdicts=verdicts) if replica is not None else []
        except (OSError, sqlite3.Error):
            _degrade()
            return []


def _replica_chats(conn: sqlite3.Connection, creator_task_id: str) -> list[dict]:
    """Standalone creation uses the same reader, scoped to this single lookup."""
    with _ReplicaReader(conn) as reader:
        return reader.resolve(creator_task_id)


def _owner_delivery(conn: sqlite3.Connection, task_id: str, owner: dict) -> None:
    """Deliver to an owner whose card is not in this file: its chat on this gateway (``tui``) or through the hub."""
    from hermes_cli.kanban_origin import gateway_names
    if owner["chat_gateway"] == gateway_names()[0]:
        platform, chat_id = "tui", owner["chat_session"]
    else:
        platform = "hub"
        chat_id = f"{owner['chat_gateway']}/{owner['chat_profile'] or 'default'}/{owner['chat_session']}"
    conn.execute(
        "INSERT OR IGNORE INTO kanban_notify_subs "
        "(task_id,platform,chat_id,thread_id,delivery_mode,delivery_metadata,created_at,last_event_id) "
        "VALUES (?,?,?,'','notify',?, unixepoch(), (SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?))",
        (task_id, platform, chat_id, json.dumps({"reason": "create"}), task_id))


def inherit_chats(conn: sqlite3.Connection, task_id: str, creator_task_id: str,
                  *, replica_reader: _ReplicaReader | None = None, verdicts: LinkVerdicts | None = None) -> None:
    """Copy a creator's real chat links inside the card creation transaction."""
    from hermes_cli.kanban_chat_links import link_uid
    from hermes_cli.kanban_db import _append_event
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        return
    from hermes_cli.kanban_owner_chat import PREFIX
    owners, source = resolve_chats(conn, creator_task_id, verdicts=verdicts), None
    if all(str(o["chat_session"] or "").startswith(PREFIX) for o in owners):
        # None, or only a producer's owner chat: a real chat on the replica (another gateway's) wins over it.
        replica = (replica_reader.resolve(creator_task_id, verdicts=verdicts) if replica_reader is not None
                   else _replica_chats(conn, creator_task_id))
        if replica and (not owners or not all(str(o["chat_session"] or "").startswith(PREFIX) for o in replica)):
            owners, source = replica, "replica"
    if not owners:
        if not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='creator_chats_unresolved'",
                            (task_id,)).fetchone():
            _append_event(conn, task_id, "creator_chats_unresolved",
                          {"task_id": task_id, "creator_task_id": creator_task_id, "max_depth": MAX_DEPTH})
        return
    _append_event(conn, task_id, "creator_chats_resolved",
                  {"creator_task_id": creator_task_id, **({"source": source} if source else {})})
    for owner in owners:
        conn.execute(
            "INSERT OR IGNORE INTO task_chat_links "
            "(uid, task_id, chat_gateway, chat_profile, chat_session, kind, released, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'created', 0, ?)",
            (link_uid(task_id, owner["chat_gateway"], owner["chat_profile"], owner["chat_session"], "created"),
             task_id, owner["chat_gateway"], owner["chat_profile"], owner["chat_session"], owner["created_at"]))
        (_owner_delivery if source else _copy_delivery)(conn, task_id, owner)


class LinkVerdicts:
    """``is_machine_session`` verdicts for every chat link ``resolve_chats`` may test in one backfill pass, read
    BEFORE the board write lock. A cold per-row lookup under ``BEGIN IMMEDIATE`` reopened every home's state.db,
    stalling the dispatcher's pull for minutes (hub, t_01a1d2cc49634a78); warming the 300 s source cache was not
    enough, since a pass that outlives it reads again (t_01a1d2f6289c3fe7). A lookup never reads state.db: a link
    that arrives mid-pass is classified only when that needs no read (another gateway's chat). A local one fails
    closed (owns nothing), so its card stays unresolved and the next pass retries it with a fresh map. [daveey]"""

    def __init__(self, verdicts: dict[tuple, bool], local_gateway):
        self.verdicts, self.local_gateway = verdicts, local_gateway

    def is_machine(self, gateway, profile, session) -> bool:
        from hermes_cli.kanban_db_notify import is_machine_session
        from hermes_cli.kanban_session_provenance import reads_local_store
        hit = self.verdicts.get((gateway, profile, str(session or "")))
        if hit is not None:
            return hit
        if reads_local_store(gateway, session, local_gateway=self.local_gateway, profile=profile):
            return True
        return is_machine_session(gateway, session, local_gateway=self.local_gateway, profile=profile)

    @classmethod
    def read(cls, conn: sqlite3.Connection, replica_reader: _ReplicaReader) -> LinkVerdicts:
        """Verdicts for the unreleased links and hub-sub origins of this board file and its replica. [daveey]"""
        from hermes_cli.kanban_origin import gateway_names, parse_origin, OriginError
        from hermes_cli.kanban_placement import is_placeholder_session
        from hermes_cli.kanban_session_provenance import machine_verdicts
        me = gateway_names()[0]
        keys: dict[tuple, None] = {}
        # Without a gateway name the replica stays unopened until a creator is missing locally (``_ReplicaReader``):
        # its links all name a gateway (``chat_gateway`` is NOT NULL in the synced schema), and with no local name
        # ``scoped_sources`` answers every such ref unknown without a read, so a lookup that misses this map gets
        # the same verdict with no state.db read (t_01a1d41a82ced7d9).
        for db in (conn, replica_reader.open() if me else None):
            try:
                if db is None or not db.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
                    continue
                for gw, prof, sid in db.execute("SELECT DISTINCT chat_gateway, chat_profile, chat_session "
                                                "FROM task_chat_links WHERE released=0"):
                    keys[(gw, prof, str(sid or ""))] = None
                for (chat_id,) in db.execute("SELECT DISTINCT chat_id FROM kanban_notify_subs WHERE platform='hub'"):
                    try:
                        origin = parse_origin(chat_id)
                    except OriginError:
                        continue
                    keys[(origin.gateway, origin.profile, str(origin.session or ""))] = None
            except sqlite3.Error:  # a missing verdict fails closed or needs no read, never a read under the lock
                _degrade()
                continue
        return cls(machine_verdicts([k for k in keys if not is_placeholder_session(k[2])], local_gateway=me), me)


def backfill(conn: sqlite3.Connection) -> dict:
    """Repair legacy worker creations once; unresolved rows remain discoverable and retryable.

    Provenance is read before ``write_txn``, in batches, and the transaction only looks verdicts up
    (:class:`LinkVerdicts`): per-row state.db reads under the board write lock held it for minutes on the hub
    (t_01a1d2cc49634a78), and a pass longer than the source cache's 300 s TTL read again. [daveey]"""
    from hermes_cli.kanban_db import write_txn
    from hermes_cli.kanban_db_notify import headless_sessions
    from hermes_cli.kanban_placement import is_placeholder_session

    from hermes_cli.kanban_creator_empty import candidates, remember_empty, write_coverage

    rows = candidates(conn)
    attempted, unresolved = [], []
    headless = headless_sessions(row["session_id"] for row in rows if row["session_id"])
    with _ReplicaReader(conn) as replica_reader:
        verdicts = (LinkVerdicts.read(conn, replica_reader) if any(
            not row["session_id"] or str(row["session_id"]) in headless or is_placeholder_session(row["session_id"])
            for row in rows) else LinkVerdicts({}, None))
        with write_txn(conn):
            covered = write_coverage(conn)
            for row in rows:
                if row["session_id"] and not (str(row["session_id"]) in headless
                                              or is_placeholder_session(row["session_id"])):
                    continue
                creator = json.loads(row["payload"] or "{}").get("creator_task_id")
                if not creator:
                    if covered:
                        remember_empty(conn, row)
                    continue
                inherit_chats(conn, row["id"], creator, replica_reader=replica_reader, verdicts=verdicts)
                attempted.append(row["id"])
                if not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='creator_chats_resolved'",
                                    (row["id"],)).fetchone():
                    unresolved.append(row["id"])
    return {"attempted": attempted, "unresolved": unresolved}
