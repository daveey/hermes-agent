"""Resolve worker provenance into durable human chat ownership. [daveey]"""
from __future__ import annotations

import json
import sqlite3

MAX_DEPTH = 16


def initialize_ownership(conn, task_id, creator_task_id, parents, created_at, chat_origin):
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

    origin = parse_origin(chat_origin) if chat_origin else None
    if origin is None or is_headless_session(origin.chat_id):
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
    local = origin.gateway == gateway_names()[0] and origin.profile == get_active_profile_name()
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


def resolve_chats(conn: sqlite3.Connection, creator_task_id: str) -> list[dict]:
    """Nearest unreleased human links; creator edges precede dependency parents, cycles stop.

    A machine session (``is_machine_session``: a worker/cron/one-shot/subagent transcript, whatever its id
    shape) owns nothing: its link is skipped and the walk goes on to the next level. [daveey]"""
    from hermes_cli.kanban_db_notify import is_machine_session
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.kanban_placement import is_placeholder_session

    me = gateway_names()[0]
    frontier, seen = [creator_task_id], set()
    for _depth in range(MAX_DEPTH):
        owners, following = {}, []
        for task_id in frontier:
            if task_id in seen:
                continue
            seen.add(task_id)
            for row in _chat_links(conn, task_id):
                if (not is_machine_session(row["chat_gateway"], row["chat_session"], local_gateway=me,
                                           profile=row["chat_profile"])
                        and not is_placeholder_session(row["chat_session"])):
                    key = (row["chat_gateway"], row["chat_profile"], row["chat_session"])
                    owners.setdefault(key, dict(row))
            row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='created' "
                               "ORDER BY id LIMIT 1", (task_id,)).fetchone()
            payload = json.loads(row[0] or "{}") if row else {}
            if payload.get("creator_task_id"):
                following.append(payload["creator_task_id"])
            following.extend(r[0] for r in conn.execute(
                "SELECT parent_id FROM task_links WHERE child_id=? ORDER BY parent_id", (task_id,)))
        if owners:
            return list(owners.values())
        frontier = list(dict.fromkeys(t for t in following if t not in seen))
        if not frontier:
            break
    return []


def _copy_delivery(conn: sqlite3.Connection, task_id: str, owner: dict) -> None:
    """Retain every delivery column, never the source card's notification cursor."""
    hub_chat = f"{owner['chat_gateway']}/{owner['chat_profile'] or 'default'}/{owner['chat_session']}"
    conn.execute(
        "INSERT OR IGNORE INTO kanban_notify_subs "
        "(task_id, platform, chat_id, thread_id, user_id, user_id_alt, chat_type, notifier_profile, "
        "delivery_mode, delivery_metadata, created_at, last_event_id) "
        "SELECT ?, platform, chat_id, thread_id, user_id, user_id_alt, chat_type, notifier_profile, "
        "delivery_mode, delivery_metadata, unixepoch(), "
        "(SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?) "
        "FROM kanban_notify_subs WHERE task_id=? AND "
        "((platform='hub' AND chat_id=?) OR (platform='tui' AND chat_id=?))",
        (task_id, task_id, owner["task_id"], hub_chat, owner["chat_session"]))


def _replica_chats(conn: sqlite3.Connection, creator_task_id: str) -> list[dict]:
    """``resolve_chats`` on this board's replica (``sync-shadow/<board>.db``, the whole board).

    A worker node runs another gateway's card from a copy in its live file: the copy carries no chat links, and its
    creator chain lives only in the replica, so a card that worker files would resolve nothing here. Read-only;
    [] when the board has no replica or it cannot be read.
    """
    from pathlib import Path
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_sync_client import shadow_path
    key = sync._db_key(conn)
    if not key or Path(key).parent.name in (sync.SHADOW_DIR, sync.LINKS_DIR):
        return []
    path = shadow_path(sync.board_slug_for_path(key))
    try:
        if not path.exists() or path.resolve() == Path(key).resolve():
            return []
        from hermes_cli import kanban_lockguard
        replica = kanban_lockguard.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except (OSError, sqlite3.Error):
        return []
    try:
        replica.row_factory = sqlite3.Row
        if not replica.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
            return []
        return resolve_chats(replica, creator_task_id)
    except sqlite3.Error:
        return []
    finally:
        replica.close()


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


def inherit_chats(conn: sqlite3.Connection, task_id: str, creator_task_id: str) -> None:
    """Copy a creator's real chat links inside the card creation transaction."""
    from hermes_cli.kanban_chat_links import link_uid
    from hermes_cli.kanban_db import _append_event
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        return
    owners, source = resolve_chats(conn, creator_task_id), None
    if not owners:
        owners = _replica_chats(conn, creator_task_id)
        source = "replica" if owners else None
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


def backfill(conn: sqlite3.Connection) -> dict:
    """Repair legacy worker creations once; unresolved rows remain discoverable and retryable."""
    from hermes_cli.kanban_db import write_txn
    from hermes_cli.kanban_db_notify import is_headless_session
    from hermes_cli.kanban_placement import is_placeholder_session

    rows = conn.execute(
        "SELECT t.id, t.session_id, e.payload FROM tasks t JOIN task_events e ON e.task_id=t.id "
        "AND e.kind='created' WHERE NOT EXISTS (SELECT 1 FROM task_events done WHERE done.task_id=t.id "
        "AND done.kind='creator_chats_resolved')").fetchall()
    attempted, unresolved = [], []
    with write_txn(conn):
        for row in rows:
            if row["session_id"] and not (is_headless_session(row["session_id"])
                                          or is_placeholder_session(row["session_id"])):
                continue
            creator = json.loads(row["payload"] or "{}").get("creator_task_id")
            if not creator:
                continue
            inherit_chats(conn, row["id"], creator)
            attempted.append(row["id"])
            if not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='creator_chats_resolved'",
                                (row["id"],)).fetchone():
                unresolved.append(row["id"])
    return {"attempted": attempted, "unresolved": unresolved}
