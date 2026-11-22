"""No parked card stays invisible: a stranded park gets one generated human ask per park episode. [daveey]

Owner decision (2026-11-22): every parked card (``triage``/``blocked``) is either red somewhere (a valid
``human_request``, shown on every chat it is linked to) or has a live owner. 61 hub cards sat in triage with neither:
spend stops routed to chats that had ended, supervisor decision cards that completed while the source stayed parked,
cards created straight into triage, transient re-kinds and containment holds.

``sweep_stranded_parks`` runs from the dispatcher's parked-card sweeps (``kanban_block_policy.sweep_blocked_cards``).
A card on this gateway's own board that has been parked longer than ``kanban.stranded_park_seconds`` with no valid
request and no live owner gets, once per park episode, a generated approval request (promote / archive / re-scope),
an optional binding to the triage desk chat (``kanban.triage_owner_chat``) and a ``stranded_park_escalated`` event.

The park episode starts at the newest park-entry event (``PARK_ENTRY_EVENTS``), never at the newest event of any
kind: a comment does not reset the clock. A live owner is an unreleased chat link whose chat is live
(``chat_live``) or an open spend-decision supervisor card for the card's current spend episode.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

EVENT = "stranded_park_escalated"
PARKED = ("triage", "blocked")
CLOSED = ("done", "archived")
# ``containment_episode`` is a tasks column, not an event kind; the containment hold's own event is ``contained``.
# Both are listed so a future event of either name opens an episode.
PARK_ENTRY_EVENTS = (
    "blocked", "gave_up", "spend_budget_triaged", "spend_budget_held", "decision_retained", "safety_refused",
    "no_progress_escalated", "agent_block_escalated", "ask_rechecked", "supervisor_escalated",
    "block_loop_detected", "containment_episode", "contained", "created",
)
DEFAULT_PARK_SECONDS = 7200
DEFAULT_OWNER_IDLE_SECONDS = 86400
DEFAULT_SWEEP_INTERVAL_SECONDS = 300
SHEPHERD_KIND = "shepherd"
CHOICES = [{"label": "Release (promote)", "value": "promote"},
           {"label": "Archive", "value": "archive"},
           {"label": "Re-scope", "value": "rescope"}]
_SPEND_KEYS = ("limit", "runs", "runs_with_usage", "cost_usd")


# --- settings ------------------------------------------------------------------------------------------------------

def _seconds(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def settings(cfg: Optional[dict] = None) -> tuple[int, int, str, int]:
    """``(stranded_park_seconds, stranded_owner_idle_seconds, triage_owner_chat, stranded_sweep_interval_seconds)``
    from ``cfg`` (the full config dict) or the on-disk config."""
    if cfg is None:
        try:
            from hermes_cli.config import load_config_readonly
            cfg = load_config_readonly() or {}
        except Exception:  # health: allow BLE001 -- unreadable config: the defaults keep the backstop running
            logger.warning("kanban: stranded-park sweep could not read config; using defaults", exc_info=True)
            cfg = {}
    kanban = (cfg.get("kanban") if isinstance(cfg, dict) else None) or {}
    desk = kanban.get("triage_owner_chat")
    return (_seconds(kanban.get("stranded_park_seconds"), DEFAULT_PARK_SECONDS),
            _seconds(kanban.get("stranded_owner_idle_seconds"), DEFAULT_OWNER_IDLE_SECONDS),
            desk.strip() if isinstance(desk, str) else "",
            _seconds(kanban.get("stranded_sweep_interval_seconds"), DEFAULT_SWEEP_INTERVAL_SECONDS))


# --- chat liveness -------------------------------------------------------------------------------------------------

def _open_store(path: Path) -> sqlite3.Connection:
    from hermes_cli import kanban_lockguard
    db = kanban_lockguard.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    return db


class Liveness:
    """Chat liveness answers for one pass (a sweep, a producer call): each ``state.db`` is opened at most once and
    each ``(profile, session)`` is judged at most once. Use as a context manager; ``close`` releases the stores."""

    def __init__(self, *, now: Optional[int] = None, idle: Optional[int] = None):
        self.now = int(now if now is not None else time.time())
        self.idle = DEFAULT_OWNER_IDLE_SECONDS if idle is None else int(idle)
        self._dbs: dict[str, Optional[sqlite3.Connection]] = {}
        self._answers: dict[tuple, bool] = {}

    def __enter__(self) -> Liveness:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        for db in self._dbs.values():
            if db is not None:
                db.close()
        self._dbs.clear()

    def _db(self, path: Path) -> Optional[sqlite3.Connection]:
        key = str(path)
        if key not in self._dbs:
            try:
                self._dbs[key] = _open_store(path)
            except (sqlite3.Error, OSError) as exc:
                logger.info("kanban: stranded-park liveness cannot read %s (%s)", path, exc)
                self._dbs[key] = None
        return self._dbs[key]

    def _state(self, path: Path, session: str):
        """True (live) / False (archived, auto-archived, hidden, or ended and idle past ``idle``) for a session found
        in ``path`` (its compression tip); ``"missing"`` when absent; None when the store is unreadable."""
        from hermes_cli.kanban_origin_adapter import _resolve_session

        db = self._db(path)
        if db is None:
            return None
        try:
            tip = _resolve_session(path, session, db)
            row = db.execute("SELECT * FROM sessions WHERE id=?", (tip,)).fetchone()
        except ValueError as exc:
            return "missing" if str(exc) == "origin_session_missing" else None
        except sqlite3.Error:
            return None
        if row is None:
            return "missing"
        data = dict(row)
        if any(data.get(flag) for flag in ("archived", "auto_archived", "hidden")):
            return False
        if data.get("ended_at") is None:
            return True
        # ``ended_at`` alone is no death: a CLI chat closed with end_reason cli_close resumes in Desktop.
        last = data.get("last_activity_at")
        last = data.get("ended_at") if last is None else last
        try:
            return self.now - float(last) <= self.idle
        except (TypeError, ValueError):
            return True

    def live(self, profile: Optional[str], session: str) -> bool:
        """Is this gateway's chat ``session`` live? Dead only when every readable store that could hold it says so
        (or none holds it); an unknown profile, a missing or unreadable store is never proof of death."""
        key = (profile, session)
        if key not in self._answers:
            self._answers[key] = self._judge(profile, session)
        return self._answers[key]

    def _judge(self, profile: Optional[str], session: str) -> bool:
        stores = _stores(profile)
        if stores is None:
            return True
        readable = False
        for path in stores:
            if not path.exists():
                continue
            state = self._state(path, session)
            if state is None:
                return True
            readable = True
            if state is True:
                return True
        return not readable


def _stores(profile: Optional[str]) -> Optional[list[Path]]:
    from hermes_cli.kanban_repin_owner import _local_stores
    if not profile:
        return _local_stores()
    from hermes_constants import get_hermes_home
    from hermes_cli.profiles import get_active_profile_name, get_profile_dir
    try:
        home = get_hermes_home() if profile == get_active_profile_name() else get_profile_dir(profile)
    except (OSError, ValueError):
        return None
    return [Path(home) / "state.db"]


def chat_live(profile: Optional[str], session: str, *, now: Optional[int] = None,
              idle: Optional[int] = None) -> bool:
    """One-off :meth:`Liveness.live`."""
    with Liveness(now=now, idle=idle) as liveness:
        return liveness.live(profile, session)


def local_chat_dead(gateway: str, profile: Optional[str], session: str, *, own: str,
                    liveness: Liveness) -> bool:
    """A chat of this gateway proven dead by the liveness rule. Another gateway's chat cannot be checked here."""
    return bool(own) and gateway == own and not liveness.live(profile, session)


# --- ownership -----------------------------------------------------------------------------------------------------

def _live_chat(conn: sqlite3.Connection, task_id: str, *, own: str, liveness: Liveness) -> Optional[str]:
    from hermes_cli.kanban_creator_chats import _chat_links
    from hermes_cli.kanban_db_notify import is_headless_session
    from hermes_cli.kanban_placement import is_placeholder_session

    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        return None
    for link in _chat_links(conn, task_id):
        gw, profile, session = link["chat_gateway"], link["chat_profile"], link["chat_session"]
        chat_id = f"{gw}/{profile or 'default'}/{session}"
        if is_placeholder_session(session) or is_headless_session(chat_id):
            continue
        if gw != own or liveness.live(profile, session):
            return chat_id
    return None


def open_decision_card(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The open spend-decision supervisor card of the card's current spend episode, if any. A done one is no owner:
    its decision did not release the source."""
    from hermes_cli.kanban_spend_decision import NOTICE, pending_episode

    stop = pending_episode(conn, task_id)
    if stop is None:
        return None
    episode = stop.get("uid") or str(stop["id"])
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? AND json_valid(payload) "
        "AND json_extract(payload,'$.episode')=? ORDER BY id DESC LIMIT 1", (task_id, NOTICE, episode)).fetchone()
    producer = _payload(row[0]).get("producer") if row else None
    if not isinstance(producer, dict) or producer.get("kind") != "supervisor" or not producer.get("task_id"):
        return None
    sup = conn.execute("SELECT status FROM tasks WHERE id=?", (producer["task_id"],)).fetchone()
    return producer["task_id"] if sup is not None and sup[0] not in CLOSED else None


def live_owner(conn: sqlite3.Connection, task_id: str, *, own: str, liveness: Liveness) -> Optional[str]:
    """What owns this parked card (``"<gw>/<profile>/<session>"`` or ``"supervisor <id>"``), else None."""
    chat = _live_chat(conn, task_id, own=own, liveness=liveness)
    if chat:
        return chat
    sup = open_decision_card(conn, task_id)
    return f"supervisor {sup}" if sup else None


# --- park episode --------------------------------------------------------------------------------------------------

def _payload(raw: Any) -> dict:
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def park_entry(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """The newest park-entry event (``{id, kind, payload, created_at}``); ``created`` counts only for a card created
    parked."""
    marks = ",".join("?" for _ in PARK_ENTRY_EVENTS)
    for row in conn.execute(f"SELECT id, kind, payload, created_at FROM task_events WHERE task_id=? "
                            f"AND kind IN ({marks}) ORDER BY id DESC", (task_id, *PARK_ENTRY_EVENTS)):
        payload = _payload(row[2])
        if row[1] == "created" and payload.get("status") not in PARKED:
            continue
        return {"id": int(row[0]), "kind": row[1], "payload": payload, "created_at": int(row[3] or 0)}
    return None


def _reason(entry: Optional[dict]) -> str:
    if entry is None:
        return "no park event recorded"
    data = entry["payload"]
    why = data.get("reason") or data.get("error") or data.get("reason_code") or "(no reason recorded)"
    return f"{entry['kind']}: {' '.join(str(why).split())[:300]}"


def _evidence(entry: Optional[dict], owner_note: str) -> str:
    parts = [f"Park-entry event {_reason(entry)}"]
    data = entry["payload"] if entry else {}
    spend = [f"{k}={data[k]}" for k in _SPEND_KEYS if data.get(k) is not None]
    if spend:
        parts.append("spend " + ", ".join(spend))
    parts.append(owner_note)
    return ". ".join(parts) + "."


def _armed_wake(conn: sqlite3.Connection, task_id: str) -> bool:
    from hermes_cli.kanban_wake_check import get_state
    return get_state(conn, task_id) is not None


def _dependency_open(conn: sqlite3.Connection, task_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM task_links l LEFT JOIN tasks p ON p.id = l.parent_id WHERE l.child_id=? "
        "AND COALESCE(p.status, '') NOT IN ('done','archived') LIMIT 1", (task_id,)).fetchone() is not None


def _leased(row: dict, now: int) -> bool:
    if row.get("current_run_id") is not None or row.get("claim_lock"):
        return True
    expires = row.get("lease_expires")
    return bool(row.get("lease_holder")) and (expires is None or int(expires) > now)


def _excluded(conn: sqlite3.Connection, row: dict, now: int) -> bool:
    from hermes_cli.kanban_open_asks import card_is_open_human_ask
    tid, kind = row["id"], row.get("block_kind")
    if _leased(row, now) or card_is_open_human_ask(row["status"], kind, row.get("human_request")):
        return True
    if kind == "time" or (kind == "dependency" and _dependency_open(conn, tid)):
        return True
    return _armed_wake(conn, tid)


# --- desk binding --------------------------------------------------------------------------------------------------

def _desk(desk: str, own: str):
    """``(origin, local)`` for the configured triage desk chat, or None."""
    if not desk:
        return None
    from hermes_cli.kanban_origin import OriginError, parse_origin
    from hermes_cli.profiles import get_active_profile_name
    try:
        origin = parse_origin(desk)
    except OriginError:
        logger.warning("kanban: kanban.triage_owner_chat %r is not <gateway>/<profile>/<session>", desk)
        return None
    return origin, bool(own) and origin.gateway == own and origin.profile == get_active_profile_name()


def _subscribe_desk(conn: sqlite3.Connection, task_id: str, desk) -> None:
    """Local delivery for the desk chat: before the escalation event, so the subscription's cursor includes it."""
    from hermes_cli.kanban_db_notify import add_notify_sub
    origin, local = desk
    add_notify_sub(conn, task_id=task_id, platform="tui" if local else "hub",
                   chat_id=origin.session if local else f"{origin.gateway}/{origin.profile}/{origin.session}",
                   delivery_metadata={"reason": SHEPHERD_KIND})


def _link_desk(conn: sqlite3.Connection, task_id: str, desk, now: int) -> None:
    """The synced ``shepherd`` link to the desk chat; this gateway's own link carries no profile, as
    ``kanban_chat_links._local_links`` wants it (else reconcile would release it). Caller holds the write txn."""
    from hermes_cli.kanban_chat_links import link_uid
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        return
    origin, local = desk
    profile = None if local else origin.profile
    uid = link_uid(task_id, origin.gateway, profile, origin.session, SHEPHERD_KIND)
    conn.execute(
        "INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, released, "
        "created_at) SELECT ?, ?, ?, ?, ?, ?, 0, ? WHERE NOT EXISTS (SELECT 1 FROM task_chat_links WHERE uid = ?)",
        (uid, task_id, origin.gateway, profile, origin.session, SHEPHERD_KIND, now, uid))
    conn.execute("UPDATE task_chat_links SET released = 0 WHERE uid = ? AND released != 0", (uid,))


# --- the sweep -----------------------------------------------------------------------------------------------------

def _request(row: dict, entry: Optional[dict], age: int, owner_note: str, kind) -> dict:
    from hermes_cli.kanban_human_request import validate_human_request
    title = " ".join(str(row.get("title") or "").split())[:200]
    return validate_human_request({
        "action": f"Decide this parked card: release it (promote), archive it, or re-scope it: {title}",
        "why_human": f"Parked {age // 3600}h with no live owner and no request ({_reason(entry)})",
        "interaction": "approval",
        "scope": "This card only",
        "evidence": _evidence(entry, owner_note),
        "choices": CHOICES,
    }, kind=kind)


def _escalate(conn: sqlite3.Connection, row: dict, entry: Optional[dict], *, now: int, age: int, desk,
              contained: bool) -> bool:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_ask_resolved import store_request
    from hermes_cli.kanban_human_request import NONHUMAN_KINDS

    tid = row["id"]
    if desk is not None:
        _subscribe_desk(conn, tid, desk)
    desk_chat = f"{desk[0].gateway}/{desk[0].profile}/{desk[0].session}" if desk is not None else None
    with kb.write_txn(conn):
        fresh = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if fresh is None:
            return False
        fresh = dict(fresh)
        if fresh["status"] not in PARKED or _excluded(conn, fresh, now) or _escalated(conn, tid, entry):
            return False
        if not contained:
            kind = fresh.get("block_kind")
            if kind in NONHUMAN_KINDS:
                kind = "needs_input"
                conn.execute("UPDATE tasks SET block_kind = ? WHERE id = ?", (kind, tid))
            store_request(conn, tid, _request(fresh, entry, age, "No unreleased chat link to a live chat and no "
                                              "open spend-decision supervisor card", kind))
        if desk is not None:
            _link_desk(conn, tid, desk, now)
        kb._append_event(conn, tid, EVENT, {
            "episode_start": entry["created_at"] if entry else int(fresh.get("created_at") or 0),
            "reason": _reason(entry), "desk_chat": desk_chat, "request": not contained})
    return True


def _escalated(conn: sqlite3.Connection, task_id: str, entry: Optional[dict]) -> bool:
    return conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=? AND id > ? LIMIT 1",
                        (task_id, EVENT, entry["id"] if entry else 0)).fetchone() is not None


# board key -> ``now`` of the last sweep that did work in this process (``sweep_stranded_parks`` throttle).
_LAST_RUN: dict[str, int] = {}


def _board_key(conn: sqlite3.Connection) -> str:
    from hermes_cli.kanban_db_connect import _main_db_file
    return _main_db_file(conn) or f"conn:{id(conn)}"


def _due(conn: sqlite3.Connection, now: int, interval: int) -> bool:
    """At most one working sweep per ``interval`` per board per process: the dispatcher ticks every 60 s and the
    per-card ownership reads (state.db) need not run that often on a loaded hub. Claims the slot when due."""
    key = _board_key(conn)
    last = _LAST_RUN.get(key)
    if last is not None and 0 <= now - last < interval:
        return False
    _LAST_RUN[key] = now
    return True


def sweep_stranded_parks(conn: sqlite3.Connection, *, now: Optional[int] = None,
                         cfg: Optional[dict] = None) -> list[str]:
    """Escalate this gateway's stranded parked cards once per park episode; returns the escalated task ids.
    Throttled to one working pass per ``kanban.stranded_sweep_interval_seconds`` per board (``_due``)."""
    from hermes_cli import kanban_containment_connections as containment
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.kanban_wake_check import foreign_ids

    park_s, idle_s, desk_ref, interval = settings(cfg)
    now = int(now if now is not None else time.time())
    if not _due(conn, now, interval):
        return []
    # A retained copy of another gateway's card has no policy authority here: only the card's home escalates it.
    copies = foreign_ids(conn)
    own = gateway_names()[0]
    desk = _desk(desk_ref, own)
    escalated: list[str] = []
    rows = [dict(r) for r in conn.execute("SELECT * FROM tasks WHERE status IN ('triage','blocked')").fetchall()]
    with Liveness(now=now, idle=idle_s) as liveness:
        for row in rows:
            tid = row["id"]
            if tid in copies or _excluded(conn, row, now):
                continue
            entry = park_entry(conn, tid)
            start = entry["created_at"] if entry else int(row.get("created_at") or 0)
            if now - start < park_s or _escalated(conn, tid, entry):
                continue
            try:
                if live_owner(conn, tid, own=own, liveness=liveness):
                    continue
                if _escalate(conn, row, entry, now=now, age=now - start, desk=desk,
                             contained=containment.held(conn, tid)):
                    escalated.append(tid)
            except Exception:  # health: allow BLE001 -- one card's failure must not stop the backstop for the rest
                logger.warning("kanban: stranded-park escalation failed for %s", tid, exc_info=True)
    return escalated
