"""A card blocked on a human always has a chat the human reads, so that chat shows it red. [daveey]

David (hub chat 20261127_220504_7d7971, 2026-11-27): "If a session is blocked on user input, then it must have a red
blocked card and the session itself must be rendered in red." Hub card t_01a21bb1169deea2 (unforged CI, PR #13) sat
``blocked`` with a valid ``human_request`` whose only creating chat was ``worker-a1/default/kanban_owner_kanban_
dispatcher``: a standing owner chat in a pool worker's state.db, which no Desktop lists. So no chat turned red.

This pass runs on the human home gateway (``kanban.human_home_gateway``, default ``hub``; any gateway of a board
with no synced chat links). For each open human ask (``card_is_open_human_ask``) with no unreleased ``created`` link
to a human chat it links one, in this order:

1. the nearest real chat up the creator chain and dependency parents (``kanban_creator_chats.resolve_chats``);
2. otherwise this gateway's standing ``Needs you`` chat (``kanban_owner_chat.ensure(NEEDS_YOU)``, pinned
   ``desktop`` session ``kanban_owner_needs_you``).

A human chat is one of this gateway's interactive chats, another gateway's non-machine chat, or the Needs-you chat
itself. A producer owner chat (``kanban_owner_<producer>``), a worker/cron transcript and a placeholder never count.
Only a ``created`` link counts, because the outcome reader drops a ``subscribed`` card's human ask (it observes, it
does not own). Existing links are left alone: this adds the human chat beside them, it never releases one. The
dispatcher runs it each tick (throttled to ``EVERY_S``); ``hermes kanban human-chats [--apply]`` runs it by hand
(the backfill).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

NEEDS_YOU = "needs_you"  # producer name of the standing chat: session ``kanban_owner_needs_you``
NEEDS_YOU_TITLE = "Needs you"
EVENT = "human_chat_linked"
DEFAULT_HOME = "hub"
EVERY_S = 60.0
_NEXT: dict[str, float] = {}


def home_gateway() -> str:
    try:
        from hermes_cli.config import load_config_readonly
        value = ((load_config_readonly() or {}).get("kanban") or {}).get("human_home_gateway")
    except Exception:  # health: allow BLE001 -- unreadable config keeps the default home
        value = None
    return str(value or DEFAULT_HOME).strip() or DEFAULT_HOME


def _links(conn) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone())


def is_home(conn) -> bool:
    """True where the pass may run: the configured home gateway, or any gateway of an unsynced board."""
    if not _links(conn):
        return True
    from hermes_cli.kanban_adoption_authority import gateway
    return gateway(conn) == home_gateway()


def needs_you_session() -> str:
    from hermes_cli.kanban_owner_chat import session_id
    return session_id(NEEDS_YOU)


def open_asks(conn) -> list[str]:
    """Home cards parked on a validated human_request (blocked/triage), oldest first."""
    from hermes_cli.kanban_open_asks import BLOCKED_STATUSES, card_is_open_human_ask
    from hermes_cli.kanban_wake_check import foreign_copy
    q = ",".join("?" * len(BLOCKED_STATUSES))
    out = []
    for tid, status, kind, request in conn.execute(
            f"SELECT id, status, block_kind, human_request FROM tasks WHERE status IN ({q}) "
            "AND human_request IS NOT NULL AND human_request != '' ORDER BY created_at", BLOCKED_STATUSES):
        if card_is_open_human_ask(status, kind, request) and not foreign_copy(conn, tid):
            out.append(tid)
    return out


def _created_chats(conn, task_id: str) -> list[tuple[str, Optional[str], str]]:
    """``(gateway, profile, session)`` of the card's unreleased creating chats (tui sub on an unsynced board)."""
    from hermes_cli.kanban_adoption_authority import gateway
    me = gateway(conn)
    rows: list[tuple[str, Optional[str], str]] = []
    session = conn.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    if session and session[0]:
        rows.append((me, None, str(session[0])))
    if _links(conn):
        rows += [(r[0], r[1], str(r[2] or "")) for r in conn.execute(
            "SELECT chat_gateway, chat_profile, chat_session FROM task_chat_links "
            "WHERE task_id=? AND kind='created' AND released=0", (task_id,))]
    else:
        from hermes_cli.kanban_db_notify import OBSERVER_SUB_REASONS
        observers = ", ".join(f"'{r}'" for r in OBSERVER_SUB_REASONS)
        rows += [(me, None, str(r[0] or "")) for r in conn.execute(
            "SELECT chat_id FROM kanban_notify_subs WHERE task_id=? AND platform='tui' AND "
            "(CASE WHEN json_valid(delivery_metadata) THEN json_extract(delivery_metadata, '$.reason') END) "
            f"NOT IN ({observers})", (task_id,))]
    return rows


def is_human_chat(conn, gateway: Any, profile: Any, session: Any) -> bool:
    from hermes_cli.kanban_adoption_authority import gateway as _gateway
    from hermes_cli.kanban_owner_chat import PREFIX
    from hermes_cli.kanban_placement import is_placeholder_session
    from hermes_cli.kanban_session_provenance import is_machine_session
    session = str(session or "")
    if not session or is_placeholder_session(session):
        return False
    me = _gateway(conn)
    if session.startswith(PREFIX):
        return session == needs_you_session() and gateway == me
    return not is_machine_session(gateway, session, local_gateway=me, profile=profile)


def has_human_chat(conn, task_id: str) -> bool:
    return any(is_human_chat(conn, *row) for row in _created_chats(conn, task_id))


def _chain_starts(conn, task_id: str) -> list[str]:
    from hermes_cli.kanban_archive_scope import _creator_task_id
    creator = _creator_task_id(conn, task_id)
    parents = [r[0] for r in conn.execute("SELECT parent_id FROM task_links WHERE child_id=? ORDER BY parent_id",
                                          (task_id,))]
    return [t for t in dict.fromkeys([creator, *parents]) if t and t != task_id]


def _chain_chat(conn, task_id: str) -> Optional[str]:
    """The creator/parent whose chain resolves a real (non owner-chat) human chat, else None."""
    if not _links(conn):
        return None
    from hermes_cli.kanban_creator_chats import resolve_chats
    from hermes_cli.kanban_owner_chat import PREFIX
    for start in _chain_starts(conn, task_id):
        owners = resolve_chats(conn, start)
        if any(not str(o["chat_session"] or "").startswith(PREFIX)
               and is_human_chat(conn, o["chat_gateway"], o["chat_profile"], o["chat_session"]) for o in owners):
            return start
    return None


def _link_chain(conn, task_id: str, start: str) -> bool:
    from hermes_cli.kanban_creator_chats import inherit_chats
    from hermes_cli.kanban_db import _append_event
    inherit_chats(conn, task_id, start)
    if not has_human_chat(conn, task_id):
        return False
    _append_event(conn, task_id, EVENT, {"source": "creator_chain", "from_task": start})
    return True


def _link_needs_you(conn, task_id: str, sid: str) -> bool:
    from hermes_cli.kanban_db import _append_event
    from hermes_cli.kanban_owner_chat import link
    link(conn, task_id, NEEDS_YOU, sid)
    if not has_human_chat(conn, task_id):
        return False
    _append_event(conn, task_id, EVENT, {"source": "needs_you", "session": sid})
    return True


def missing(conn) -> list[str]:
    """Open human asks with no human chat (read-only)."""
    return [tid for tid in open_asks(conn) if not has_human_chat(conn, tid)]


def sweep(conn, *, apply: bool = True, only: Optional[Iterable[str]] = None) -> dict:
    """Link a human chat to each open human ask that lacks one (``only``: just these cards). Returns
    ``{"open_asks", "missing", "linked": [{"task", "source", "session"}], "failed", "refreshed"}``."""
    from hermes_cli import kanban_db as kb
    asks = open_asks(conn)
    if only is not None:
        wanted = set(only)
        asks = [t for t in asks if t in wanted]
    gaps = [t for t in asks if not has_human_chat(conn, t)]
    result: dict[str, Any] = {"open_asks": len(asks), "missing": list(gaps), "linked": [], "failed": [],
                              "refreshed": 0, "home": is_home(conn)}
    if not apply or not gaps or not result["home"]:
        return result
    sid: Optional[str] = None
    touched: set[str] = set()
    for tid in gaps:
        start = _chain_chat(conn, tid)
        if start is None and sid is None:
            try:
                from hermes_cli.kanban_owner_chat import ensure
                sid = ensure(NEEDS_YOU)  # state.db work outside the kanban write lock
            except Exception:  # health: allow BLE001 -- the next pass retries; the card keeps its links
                log.warning("kanban: Needs-you chat unavailable; %s waits for the next pass", tid, exc_info=True)
                result["failed"].append(tid)
                continue
        with kb.write_txn(conn):
            if has_human_chat(conn, tid):
                continue
            ok = _link_chain(conn, tid, start) if start else _link_needs_you(conn, tid, sid or "")
        if not ok:
            result["failed"].append(tid)
            continue
        session = next((s for g, p, s in _created_chats(conn, tid) if is_human_chat(conn, g, p, s)), "")
        result["linked"].append({"task": tid, "source": "creator_chain" if start else "needs_you",
                                 "session": session})
        touched.add(session)
    if touched:
        try:
            from hermes_state_outcome import refresh_outcomes_quietly
            result["refreshed"] = refresh_outcomes_quietly(sorted(s for s in touched if s), EVENT)
        except Exception:  # health: allow BLE001 -- the next card event refreshes the outcome anyway
            log.warning("kanban: outcome refresh after human-chat link failed", exc_info=True)
    return result


def maybe_sweep(conn, *, now: float) -> Optional[dict]:
    """Dispatcher maintenance on the home gateway, at most every ``EVERY_S``."""
    path = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    if now < _NEXT.get(path, float("-inf")):
        return None
    _NEXT[path] = now + EVERY_S
    from hermes_cli.kanban_owner_chat import enabled
    if not enabled() or not is_home(conn):  # ``kanban.owner_chats: false`` turns standing chats off, this one too
        return None
    result = sweep(conn, apply=True)
    if result["linked"]:
        log.info("kanban human chats: linked %s", result["linked"])
    return result


def command(args) -> int:
    from hermes_cli import kanban_db_connect as kbc
    apply = bool(getattr(args, "apply", False))
    with kbc.connect_closing() as conn:
        result = sweep(conn, apply=apply)
        result["after_missing"] = missing(conn) if apply else result["missing"]
    if getattr(args, "json", False):
        print(json.dumps(result, sort_keys=True))
        return 0
    print(f"{result['open_asks']} open human asks; {len(result['missing'])} had no human chat.")
    for item in result["linked"]:
        print(f"  linked {item['task']} -> {item['session']} ({item['source']})")
    if not apply and result["missing"]:
        print("  pass --apply to link them" + ("" if result["home"] else f" (on {home_gateway()})"))
    for tid in result.get("failed") or ():
        print(f"  FAILED {tid}")
    return 1 if apply and result["after_missing"] else 0
