"""Done/blocked notices for cards on OTHER gateways that a chat here is linked to. [daveey]

A chat's local cards notify it through ``platform='tui'`` subscriptions (``session_notifications._kb_poll_board``).
A card placed on another gateway (default ``kanban.placement`` route, deferred forward, ``hermes-hub create-task``,
a cross_task shepherd) is linked to the chat by a ``platform='hub'`` subscription on the OWNER's board instead. The
owner's hermes-hub client publishes that link as ``sessions: [{gw, profile, session, rel: 'shepherd'}]`` on the
card's task record, and this gateway's replica (``~/.local/state/hermes-hub/hub.db``) carries it with the card's
status. Nothing turned that link into a notice: a chat whose card ran on the hub never heard it finished, so it
never verified the work (hub kanban.db evidence 2026-10-28, card t_01a182faed899158).

This module reads the replica for cards linked to (this gateway, the session's profile, the session) and returns
one notice per terminal state (done, blocked), claimed in a small SQLite ledger so a notice is delivered once
across every backend on this host. A refused turn releases its claim (``release``) so the owner of the chat
re-claims it. Only states that became terminal within ``FRESH_S`` are announced; older ones are recorded
silently, so a first run never floods a chat with history.

Without hermes-hub (no hub.db) everything here is a no-op.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

FRESH_S = 6 * 3600.0
MAX_PER_POLL = 10
LINK_RELS = ("shepherd",)
_TERMINAL = ("done", "blocked")

_lock = threading.Lock()
_index_memo: Optional[tuple] = None  # (path, signature, me, index)


def hub_db_path() -> Path:
    return Path(os.environ.get("HERMES_HUB_STATE") or Path.home() / ".local" / "state" / "hermes-hub") / "hub.db"


def ledger_path() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "kanban" / "remote-notices.db"


def _ro(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn
    except sqlite3.OperationalError:
        conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=1.0)
        conn.execute("PRAGMA query_only = ON")
        return conn


def _terminal_at(task: dict) -> Optional[float]:
    run = task.get("run") if isinstance(task.get("run"), dict) else {}
    for value in (task.get("completed_at") if task.get("status") == "done" else None, run.get("ended_at")):
        with contextlib.suppress(TypeError, ValueError):
            if value:
                return float(value)
    return None


def _index(path: Path) -> tuple[str, dict]:
    """``(this gateway, {(profile, session): [card, ...]})`` for terminal cards owned by other gateways, cached on
    the replica's task-row signature (one decode per change, shared by every chat's poll)."""
    global _index_memo
    conn = _ro(path)
    try:
        row = conn.execute("SELECT v FROM meta WHERE k = 'gateway'").fetchone()
        me = str(row[0]) if row and row[0] else ""
        sig = conn.execute("SELECT COUNT(*), MAX(seq), TOTAL(seq) FROM replica WHERE coll = 'tasks'").fetchone()
        memo = _index_memo
        if memo is not None and memo[0] == str(path) and memo[1] == sig and memo[2] == me:
            return me, memo[3]
        rows = conn.execute("SELECT gw, key, data FROM replica WHERE coll = 'tasks'").fetchall()
    finally:
        conn.close()
    index: dict[tuple[str, str], list[dict]] = {}
    for gw, key, data in rows:
        if not me or gw == me:
            continue  # this gateway's own cards notify through local tui subscriptions
        try:
            task = json.loads(data)
        except (TypeError, ValueError):
            continue
        if not isinstance(task, dict) or task.get("status") not in _TERMINAL or not task.get("id"):
            continue
        links = task.get("sessions") if isinstance(task.get("sessions"), list) else []
        for link in links:
            if (isinstance(link, dict) and link.get("gw") == me and link.get("rel") in LINK_RELS
                    and link.get("session")):
                index.setdefault((str(link.get("profile") or "default"), str(link["session"])), []).append(
                    {"gw": str(gw), "key": str(key), "task": task})
    _index_memo = (str(path), sig, me, index)
    return me, index


def _ledger(path: Optional[Path] = None) -> sqlite3.Connection:
    path = path or ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS notices (session TEXT NOT NULL, ref TEXT NOT NULL, state TEXT NOT NULL, "
                 "at REAL NOT NULL, delivered INTEGER NOT NULL, PRIMARY KEY (session, ref, state))")
    return conn


def _first_line(value: Any, limit: int) -> str:
    lines = str(value or "").strip().splitlines()
    return f"\n{lines[0][:limit]}" if lines else ""


def format_notice(gw: str, key: str, task: dict) -> str:
    """Same shape as a local notice (``session_notifications._format_kanban_event_text``), plus where it ran."""
    parts = key.split("/")
    board = parts[-2] if len(parts) >= 2 else "default"
    tid = str(task.get("id"))
    title = str(task.get("title") or tid)[:120]
    who = task.get("assignee") or ""
    run = task.get("run") if isinstance(task.get("run"), dict) else {}
    prefix = f"[{gw}:{board}] " + (f"@{who} " if who else "")
    if task.get("status") == "done":
        handoff = _first_line(run.get("summary") or task.get("result"), 200)
        return f"✔ {prefix}Kanban {tid} done — {title}{handoff}\n(ran on {gw}; read it with cross_task @task:{board}@{gw}/{tid})"
    reason = run.get("summary") or task.get("block_kind") or ""
    return (f"⏸ {prefix}Kanban {tid} blocked" + (f": {str(reason)[:160]}" if reason else "")
            + f" — {title}\n(on {gw}; read it with cross_task @task:{board}@{gw}/{tid})")


def collect(session_key: str, profile: str, *, hub_db: Optional[Path] = None, ledger: Optional[Path] = None,
            now: Optional[float] = None, lineage: tuple[str, ...] = (),
            skip: Optional[Callable[[str], bool]] = None) -> list[dict]:
    """Claim and return ``[{text, session, ref, state}]`` for this session's newly terminal remote cards.

    ``lineage`` adds earlier ids of the same chat (a compaction fork keeps the link on the old id). ``skip(task_id)``
    is true for remote cards a local placeholder already reports (a deferred placement forward): its own tui
    subscription announces the result, so a second notice would be a duplicate. Never raises."""
    path = hub_db or hub_db_path()
    if not session_key or not path.is_file():
        return []
    now = time.time() if now is None else now
    try:
        with _lock:
            _, index = _index(path)
        cards = []
        for sid in (session_key, *lineage):
            cards.extend(index.get((profile or "default", sid), ()))
        if not cards:
            return []
        out: list[dict] = []
        conn = _ledger(ledger)
        try:
            for card in cards:
                task = card["task"]
                ref = f"{card['gw']}/{card['key']}"
                at = _terminal_at(task)
                state = f"{task['status']}@{int(at) if at else 0}"
                fresh = at is not None and now - at <= FRESH_S
                if fresh and skip is not None and conn.execute(
                        "SELECT 1 FROM notices WHERE session=? AND ref=? AND state=?",
                        (session_key, ref, state)).fetchone() is None and skip(str(task["id"])):
                    fresh = False  # recorded silently: the placeholder's own notice covers it
                with conn:
                    cur = conn.execute("INSERT OR IGNORE INTO notices VALUES (?,?,?,?,?)",
                                       (session_key, ref, state, now, 1 if fresh else 0))
                if cur.rowcount == 1 and fresh:
                    out.append({"text": format_notice(card["gw"], card["key"], task), "session": session_key,
                                "ref": ref, "state": state})
                    if len(out) >= MAX_PER_POLL:
                        break
        finally:
            conn.close()
        return out
    except Exception:
        return []


def release(claim: dict, *, ledger: Optional[Path] = None) -> bool:
    """Hand a refused notice back so the chat's owning backend claims it on its next poll."""
    try:
        conn = _ledger(ledger)
        try:
            with conn:
                cur = conn.execute("DELETE FROM notices WHERE session=? AND ref=? AND state=?",
                                   (claim["session"], claim["ref"], claim["state"]))
            return cur.rowcount == 1
        finally:
            conn.close()
    except Exception:
        return False


def _reset_for_tests() -> None:
    global _index_memo
    _index_memo = None
