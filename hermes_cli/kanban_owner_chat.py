"""Every card has an owning chat: one standing "Kanban owner" chat per producer. [daveey]

A card is renewable, re-budgetable and archivable only by its owning chat, its running creator task or a human
operator (``kanban_scope_renewal``). Cards from monitors, watchers, detectors and terminals had none, so a scope or
budget stop on them waited for David by hand (1,894 of 3,838 cards in the week to 2026-11-24).

At creation (``kanban_db.create_task``, inside its write transaction) a card that has no durable owner after the
usual checks gets its producer's owner chat: a pinned ``desktop`` session titled ``Kanban owner: <producer>`` in
this profile's state.db, created on first use (seeded with a message saying what it is for, so it is neither an
empty draft nor a context-free turn when woken) and reused after. It becomes the card's creating chat exactly like
a registered chat anchored by ``kanban_create_authority`` (a ``created`` link plus ``creator_chats_resolved`` on a
synced board, a ``tui`` subscription otherwise), so link reconciliation keeps it and decision routing binds it.

* System creators (``require_scope_authority=False``): the producer is ``owner_producer`` or ``created_by``. If the
  owner chat cannot be made (state.db unavailable), the card is created ownerless with an
  ``owner_chat_unavailable`` event and ``hermes kanban owner-chats --apply`` owns it later.
* Agent/CLI ingress (``require_scope_authority=True``): the caller's own chat or creator task as before; with
  neither, only a producer the caller's runner declared (the cron job, or ``HERMES_KANBAN_PRODUCER`` outside any
  agent turn) gets an owner chat. Otherwise the create is refused (``scope_authority_missing``).
* ``kanban.owner_chats: false`` restores the old behaviour exactly.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Optional

log = logging.getLogger(__name__)

PREFIX = "kanban_owner_"
SOURCE = "desktop"  # interactive: a machine source cannot own a card, and the sidebar lists desktop chats
EVENT = "owner_chat_attached"
UNAVAILABLE = "owner_chat_unavailable"
ENV = "HERMES_KANBAN_PRODUCER"
TERMINAL = "terminal"
_GENERATIONS = 10  # a user-deleted owner chat's id is retired for good: the next one takes a fresh suffix
RECHECK_S = 600.0  # a cached owner chat is re-validated (archived? deleted?) at most this often per process
BACKFILL_EVERY_S = 3600.0
_ENSURED: dict[tuple[str, str], tuple[str, float]] = {}
_LOCK = threading.Lock()
_LAST_BACKFILL: dict[str, float] = {}


def enabled() -> bool:
    try:
        from hermes_cli.config import load_config_readonly
        return bool(((load_config_readonly() or {}).get("kanban") or {}).get("owner_chats", True))
    except Exception:  # health: allow BLE001 -- create runs this in its write txn; unreadable config keeps the default
        log.warning("kanban.owner_chats unreadable; keeping owner chats on", exc_info=True)
        return True


def slug(producer: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(producer or "").lower()).strip("_")[:60] or "unattributed"


def session_id(producer: str, generation: int = 1) -> str:
    return PREFIX + slug(producer) + ("" if generation == 1 else f"_{generation}")


def title(producer: str) -> str:
    name = str(producer or "").strip() or "unattributed"
    if name.startswith("cron:"):
        from cron.jobs import get_job
        job = get_job(name[5:]) or {}
        if job.get("name"):
            name = f"cron {job['name']}"
    return f"Kanban owner: {name}"


def brief(producer: str) -> str:
    return (f"I am the standing owner chat for Kanban cards created by `{producer}`. Those cards have no other chat, "
            "so when one stops (spend budget, scope stop) or needs a decision, the notice comes here and I decide: "
            "release it with a goal-level mandate (`hermes kanban release <id> --note ...`), re-scope it, or archive "
            "it when it is obsolete. I read the card, its runs and comments before deciding, and I never lift a "
            "provider safety refusal.")


def declared_producer(tool_task_id: Optional[str] = None, *, allow_env: bool = True) -> Optional[str]:
    """The producer a headless caller's runner declared: the cron job running a tool call, else (outside any
    agent turn only) ``HERMES_KANBAN_PRODUCER``, which the cron script runner sets."""
    parts = str(tool_task_id or "").split(":")
    if len(parts) >= 2 and parts[0] == "cron" and parts[1]:
        return "cron:" + parts[1]
    if not allow_env:
        return None
    return (os.environ.get(ENV) or "").strip() or None


def tool_create_options(tool_task_id: Optional[str], selfwork_intent) -> dict:
    """``create_task`` owner options for the ``kanban_create`` tool. Receiver-set self-work intake is chat-less
    by contract, so it gets no owner chat. Any other chat-less caller is owned by its runner's producer: the cron
    job running this tool call, or (only outside an agent turn: a script calling the tool directly)
    ``HERMES_KANBAN_PRODUCER``."""
    return {"settle_owner_chat": selfwork_intent is None,
            "owner_producer": declared_producer(tool_task_id, allow_env=not tool_task_id)}


def _open_store(path: str):
    from hermes_state import SessionDB
    return SessionDB(db_path=path)


def ensure(producer: str) -> str:
    """The producer's live owner chat in this profile's state.db (created, pinned and un-archived as needed).

    Within ``RECHECK_S`` of the last check the cached id is returned without touching state.db, so a create
    holding the kanban write lock pays for state.db once per producer per process, not per card."""
    import time

    from hermes_constants import get_hermes_home
    from hermes_state_runtime import RuntimeStoreError
    path = str(get_hermes_home() / "state.db")
    key = (path, slug(producer))
    with _LOCK:
        cached = _ENSURED.get(key)
        if cached and time.monotonic() - cached[1] < RECHECK_S:
            return cached[0]
        db = _open_store(path)
        try:
            for generation in range(1, _GENERATIONS + 1):
                sid = session_id(producer, generation)
                try:
                    db.ensure_session(sid, SOURCE)
                except RuntimeStoreError as exc:
                    if exc.reason == "not_found":
                        continue  # deleted by the user: retired for good, take the next generation
                    raise
                break
            else:
                raise RuntimeError(f"every owner chat id for producer {producer!r} was deleted")
            row = db.get_session(sid) or {}
            if not row.get("message_count"):
                db.append_message(sid, "assistant", brief(producer))
            if not db.get_session_title(sid):
                try:
                    db.set_session_title(sid, title(producer))
                except ValueError:  # a title clash costs only the label
                    log.warning("kanban owner chat %s: title %r refused", sid, title(producer))
            if row.get("archived"):
                db.set_session_archived(sid, False, notify=False)
            if not row.get("pinned"):
                db.set_session_pinned(sid, True)  # pins are exempt from the draft and idle archive sweeps
        finally:
            db.close()
        from hermes_cli import kanban_session_provenance as prov
        prov._source_cache.pop((path, sid), None)  # a cached "missing" would hide the new chat for 300 s
        _ENSURED[key] = (sid, time.monotonic())
    return sid


def attach(conn, task_id: str, producer: str) -> str:
    """Inside create's transaction: make the producer's owner chat this card's creating chat."""
    return link(conn, task_id, producer, ensure(producer))


def link(conn, task_id: str, producer: str, sid: str) -> str:
    """Kanban-side half of :func:`attach`: link and record ``sid`` (an ensured owner chat) as the creating chat."""
    from hermes_cli.kanban_adoption_authority import gateway as _gateway
    from hermes_cli.kanban_chat_links import link_uid
    from hermes_cli.kanban_db import _append_event
    from hermes_cli.profiles import get_active_profile_name

    me, profile = _gateway(conn), get_active_profile_name()
    chat_profile = None if profile == "default" else profile  # the UID ordinary reconciliation derives
    created = conn.execute("SELECT created_at FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        conn.execute(
            "INSERT OR IGNORE INTO task_chat_links "
            "(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
            "VALUES (?,?,?,?,?,'created',0,?)",
            (link_uid(task_id, me, chat_profile, sid, "created"), task_id, me, chat_profile, sid, created))
        # What reconciliation reads to keep a created link (kanban_chat_links._local_links); a stop then binds
        # the chat's subscription (kanban_spend_decision._route), so no event wakes it before a decision.
        _append_event(conn, task_id, "creator_chats_resolved",
                      {"registered_session": sid, "chat_gateway": me, "chat_profile": chat_profile})
    else:
        # A board without links: its owning chat IS its tui subscription (kanban_create_authority legacy branch).
        conn.execute(
            "INSERT OR IGNORE INTO kanban_notify_subs "
            "(task_id,platform,chat_id,thread_id,delivery_mode,delivery_metadata,created_at,last_event_id) "
            "VALUES (?,'tui',?,'','notify','{\"reason\":\"owner\"}',?, "
            "(SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?))",
            (task_id, sid, created, task_id))
    _append_event(conn, task_id, EVENT, {"producer": producer, "session": sid, "chat_gateway": me,
                                         "chat_profile": chat_profile})
    return sid


def settle(conn, task_id: str, creator_task_id: Optional[str], *, created_by: Optional[str], strict: bool,
           producer: Optional[str] = None) -> None:
    """Leave the card with a durable owner, or refuse the create (raises ``CreateAuthorityRefused``)."""
    from hermes_cli import kanban_create_authority as kca
    if not enabled():
        if strict:
            kca.require(conn, task_id, creator_task_id)
        return
    if kca.has_authority(conn, task_id, creator_task_id, durable=True):
        return
    kca._anchor_registered_session(conn, task_id)
    if kca.has_authority(conn, task_id, creator_task_id, durable=True):
        return
    name = (producer or "").strip() or (None if strict else (created_by or "").strip() or "unattributed")
    if not name:
        kca.require(conn, task_id, creator_task_id)  # raises: no chat, no creator, no declared producer
        return
    try:
        sid = ensure(name)  # state.db only; the kanban writes below are not covered by this fallback
    except Exception as exc:  # health: allow BLE001 -- state.db trouble must not stop a monitor filing its card
        if strict:
            raise kca.CreateAuthorityRefused(
                f"the owner chat for producer {name!r} could not be made ({exc}); nothing was created.") from exc
        from hermes_cli.kanban_db import _append_event
        log.warning("kanban owner chat for %s unavailable; card %s created ownerless", name, task_id, exc_info=True)
        _append_event(conn, task_id, UNAVAILABLE, {"producer": name, "error": str(exc)[:500]})
        return
    link(conn, task_id, name, sid)
    if not kca.has_authority(conn, task_id, creator_task_id, durable=True):
        raise kca.CreateAuthorityRefused(
            f"the owner chat for producer {name!r} could not be linked as this card's owner; nothing was created.")


def _owner_refs(conn, task_id: str, links: bool) -> tuple[list[str], bool]:
    """``(owner chat ids, has another owning chat)`` among this card's unreleased creating chats."""
    if links:
        rows = [r[0] for r in conn.execute(
            "SELECT chat_session FROM task_chat_links WHERE task_id=? AND kind='created' AND released=0", (task_id,))]
    else:
        rows = [r[0] for r in conn.execute(
            "SELECT chat_id FROM kanban_notify_subs WHERE task_id=? AND platform='tui'", (task_id,))]
    mine = [str(r) for r in rows if str(r or "").rsplit("/", 1)[-1].startswith(PREFIX)]
    return mine, len(mine) < len(rows)


def _needs_owner(conn, task_id: str, links: bool, live) -> Optional[list[str]]:
    """None when the card is owned; else the dead owner chats to retire before it gets a live one."""
    from hermes_cli import kanban_create_authority as kca
    if links and conn.execute("SELECT 1 FROM task_chat_links WHERE task_id=? AND kind='created' AND released=1",
                              (task_id,)).fetchone():
        return None  # its owner let go on purpose; re-owning it would undo that
    mine, other = _owner_refs(conn, task_id, links)
    if mine and not other and not any(live(ref.rsplit("/", 1)[-1]) for ref in mine):
        return mine  # owned only by owner chats the user deleted or archived: delivery would go nowhere
    if kca.has_authority(conn, task_id, None, durable=True):
        return None
    return []


def _liveness():
    from hermes_constants import get_hermes_home
    db = _open_store(str(get_hermes_home() / "state.db"))
    seen: dict[str, bool] = {}

    def live(sid: str) -> bool:
        if sid not in seen:
            row = db.get_session(sid)
            seen[sid] = bool(row) and not row.get("archived")
        return seen[sid]
    return db, live


def ownerless(conn, live=None) -> list[tuple[str, str]]:
    """``(task_id, producer)`` for open home cards with no live owner (read-only)."""
    from hermes_cli.kanban_wake_check import foreign_copy
    links = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone())
    db = None
    if live is None:
        db, live = _liveness()
    try:
        out = []
        for task_id, created_by in conn.execute(
                "SELECT id, created_by FROM tasks WHERE status NOT IN ('done','archived') "
                "ORDER BY created_at").fetchall():
            if foreign_copy(conn, task_id) or _needs_owner(conn, task_id, links, live) is None:
                continue
            out.append((task_id, (created_by or "").strip() or "unattributed"))
        return out
    finally:
        if db is not None:
            db.close()


def backfill(conn, *, apply: bool) -> dict:
    """Give each ownerless open card its producer's owner chat (``apply``), re-checked under the write lock.
    Links to owner chats the user deleted or archived are released (or unsubscribed) first."""
    from hermes_cli import kanban_db as kb
    links = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone())
    db, live = _liveness()
    try:
        found, linked = ownerless(conn, live), []
        if apply:
            for task_id, producer in found:
                sid = ensure(producer)  # state.db work outside the kanban write lock
                with kb.write_txn(conn):
                    dead = _needs_owner(conn, task_id, links, live)
                    if dead is None:
                        continue
                    for ref in dead:
                        if ref.rsplit("/", 1)[-1] == sid:
                            continue  # an archived chat ensure() just restored: keep its link
                        if links:
                            conn.execute("UPDATE task_chat_links SET released=1 WHERE task_id=? AND kind='created' "
                                         "AND chat_session=?", (task_id, ref))
                        else:
                            conn.execute("DELETE FROM kanban_notify_subs WHERE task_id=? AND platform='tui' "
                                         "AND chat_id=?", (task_id, ref))
                    link(conn, task_id, producer, sid)
                    linked.append(task_id)
    finally:
        db.close()
    by_producer: dict[str, int] = {}
    for _, producer in found:
        by_producer[producer] = by_producer.get(producer, 0) + 1
    return {"ownerless": len(found), "linked": len(linked), "by_producer": by_producer}


def maybe_backfill(conn, *, now: float) -> Optional[dict]:
    """Dispatcher maintenance: at most hourly per board per process, own whatever is still ownerless (a create
    that found state.db unavailable, an owner chat the user deleted or archived)."""
    path = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    if not enabled() or now - _LAST_BACKFILL.get(path, float("-inf")) < BACKFILL_EVERY_S:
        return None
    _LAST_BACKFILL[path] = now
    result = backfill(conn, apply=True)
    if result["linked"]:
        log.info("kanban owner chats: linked %d ownerless cards %s", result["linked"], result["by_producer"])
    return result


def command(args) -> int:
    import json

    from hermes_cli import kanban_db_connect as kbc
    apply = bool(getattr(args, "apply", False))
    with kbc.connect_closing() as conn:
        result = backfill(conn, apply=apply)
    if getattr(args, "json", False):
        print(json.dumps(result, sort_keys=True))
        return 0
    done = f"linked {result['linked']}" if apply else "pass --apply to link them"
    print(f"{result['ownerless']} open cards have no owning chat; {done}.")
    for producer, n in sorted(result["by_producer"].items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {producer}")
    return 0
