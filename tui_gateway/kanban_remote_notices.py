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
across every backend on this host. A claim is in flight (holder pid + process start, the same probe the local
tui path uses) until the notice is in the chat transcript; only then is the row ``delivered``. A holder that
died first is re-offered, unless that key is already in the transcript. A refused turn releases its claim
(``release``) so the owner of the chat re-claims it. Only states that became terminal within ``FRESH_S`` are
announced; older ones are recorded silently, so a first run never floods a chat with history. A legacy
``delivered=1`` row from before this (claimed before the write) inside ``FRESH_S`` whose notice is in no
transcript of the chat is re-offered once.

Without hermes-hub (no hub.db) everything here is a no-op.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
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
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS notices (session TEXT NOT NULL, ref TEXT NOT NULL, state TEXT NOT NULL, "
                 "at REAL NOT NULL, delivered INTEGER NOT NULL, PRIMARY KEY (session, ref, state))")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(notices)")}
    for name, decl in (("holder_pid", "INTEGER"), ("holder_start", "INTEGER"), ("notice_key", "TEXT"),
                       ("legacy_seen", "INTEGER NOT NULL DEFAULT 0")):
        if name not in cols:
            conn.execute(f"ALTER TABLE notices ADD COLUMN {name} {decl}")
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


def _event_key(owner: str, key: str, notice: dict) -> str:
    """Event identity shared by the poller (``collect``) and the sweeper, so a chat gets one notice per event."""
    return f"remote:{owner}:{key}:{notice['event_id']}"


def _shared_key(owner: str, key: str, task: dict, profile: str, sid: str) -> str:
    """The ``kanban_event_keys`` value both paths record. Done uses :func:`_event_key`; blocked has no
    completion event, so the terminal state plus the destination is the identity."""
    notice = task.get("completion_notice")
    if task.get("status") == "done" and isinstance(notice, dict) and "event_id" in notice:
        return _event_key(owner, key, notice)
    at = _terminal_at(task)
    return f"remote:{owner}:{key}:{task.get('status')}@{int(at) if at else 0}:{profile}:{sid}"


def _my_holder() -> tuple:
    from hermes_cli.kanban_db_notify import _my_holder as holder
    return holder()


def _holder_is_me(pid) -> bool:
    from hermes_cli.kanban_db_notify import _holder_is_me
    return pid is not None and _holder_is_me({"inflight_pid": pid})


def _holder_alive(pid, start) -> bool:
    from hermes_cli.kanban_db_notify import _holder_alive
    return pid is not None and _holder_alive({"inflight_pid": pid, "inflight_start": start})


def notice_in_transcript(db, session_id: str, notice_key: str, task_id: str) -> bool:
    """True when this chat's compression lineage already has the notice (keyed row, or a legacy
    ``Kanban <id>`` line). Reuses ``kanban_notice_keys_delivered`` — the local at-least-once dedup."""
    if db is None:
        return False
    if notice_key and hasattr(db, "kanban_notice_keys_delivered"):
        if notice_key in db.kanban_notice_keys_delivered(session_id, [notice_key]):
            return True
    if not task_id or not hasattr(db, "_read_ctx"):
        return False
    from hermes_state_sessions import _LINEAGE_CTE_SQL
    with db._read_ctx() as conn:
        row = conn.execute(
            _LINEAGE_CTE_SQL + " SELECT 1 FROM messages m WHERE m.session_id IN (SELECT id FROM lineage) "
            "AND instr(m.content, ?) > 0 LIMIT 1",
            (session_id, session_id, f"Kanban {task_id} ")).fetchone()
    return row is not None


def _completion_text(gw: str, key: str, task: dict, me: str, profile: str, sid: str) -> Optional[str]:
    """Standard done notice from the owner's published ``completion_notice`` (FINAL SEND aware), else None."""
    notice = task.get("completion_notice")
    if task.get("status") != "done" or not isinstance(notice, dict) or "event_id" not in notice:
        return None
    from tui_gateway.session_notifications import _format_kanban_event_text
    sub = {"task_id": task["id"], "chat_id": f"{me}/{profile}/{sid}"}
    ev = SimpleNamespace(kind="completed", payload=notice.get("payload"))
    return _format_kanban_event_text(sub, SimpleNamespace(**task), ev, task.get("board", "default"))


def _notice_item(card: dict, session_key: str, me: str, profile: str) -> dict:
    task = card["task"]
    at = _terminal_at(task)
    sid = card.get("sid") or session_key
    text = _completion_text(card["gw"], card["key"], task, me, profile, sid)
    item = {"text": text or format_notice(card["gw"], card["key"], task), "session": session_key,
            "ref": f"{card['gw']}/{card['key']}", "state": f"{task['status']}@{int(at) if at else 0}"}
    if text:
        item["event_key"] = _event_key(card["gw"], card["key"], task["completion_notice"])
    elif task.get("status") == "blocked":
        item["event_key"] = _shared_key(card["gw"], card["key"], task, profile, sid)
    return item


def _reconcile_legacy(conn, session_key: str, cards: list, now: float, transcript_has, me: str,
                      profile: str) -> list:
    """Re-offer a pre-fix ``delivered=1`` row once, if it is still inside ``FRESH_S`` and in no transcript."""
    if transcript_has is None:
        return []
    rows = conn.execute(
        "SELECT ref, state, at FROM notices WHERE session=? AND delivered=1 AND IFNULL(legacy_seen,0)=0 "
        "AND notice_key IS NULL", (session_key,)).fetchall()
    if not rows:
        return []
    by_state = {}
    for card in cards:
        task = card["task"]
        at = _terminal_at(task)
        by_state[(f"{card['gw']}/{card['key']}", f"{task['status']}@{int(at) if at else 0}")] = card
    pid, start = _my_holder()
    stale = 0
    out = []
    for row in rows:
        card = by_state.get((row["ref"], row["state"]))
        if card is None or now - float(row["at"] or 0) > FRESH_S:
            stale += 1
            conn.execute("UPDATE notices SET legacy_seen=1 WHERE session=? AND ref=? AND state=?",
                         (session_key, row["ref"], row["state"]))
            continue
        task = card["task"]
        key = _shared_key(card["gw"], card["key"], task, profile, card.get("sid") or session_key)
        if transcript_has(key, str(task["id"])):
            conn.execute("UPDATE notices SET legacy_seen=1, notice_key=? WHERE session=? AND ref=? AND state=?",
                         (key, session_key, row["ref"], row["state"]))
            continue
        cur = conn.execute(
            "UPDATE notices SET delivered=0, holder_pid=?, holder_start=?, notice_key=?, legacy_seen=1 "
            "WHERE session=? AND ref=? AND state=? AND delivered=1 AND IFNULL(legacy_seen,0)=0",
            (pid, start, key, session_key, row["ref"], row["state"]))
        if cur.rowcount == 1:
            out.append(_notice_item(card, session_key, me, profile))
    if stale:
        logger.info("remote kanban: left %d delivered notice(s) unreoffered "
                    "(older than %.0fh, or not in the replica)", stale, FRESH_S / 3600)
    if out:
        logger.info("remote kanban: re-offered %d legacy notice(s) claimed delivered but absent "
                    "from the transcript", len(out))
    return out


def _claim_notice(conn, *, session, ref, state, now, fresh, notice_key, task_id, transcript_has) -> str:
    """``offer`` a new or orphaned claim, ``acked`` when the transcript already has it, else ``skip``.

    A fresh claim is in flight (delivered=0, this process's pid + start). It becomes delivered only in
    :func:`ack`, after the transcript write. A holder that is gone is re-offered unless the key is already
    in the transcript.
    """
    row = conn.execute(
        "SELECT delivered, holder_pid, holder_start, notice_key FROM notices WHERE session=? AND ref=? AND state=?",
        (session, ref, state)).fetchone()
    pid, start = _my_holder()
    if row is None:
        with conn:
            conn.execute(
                "INSERT INTO notices (session, ref, state, at, delivered, holder_pid, holder_start, notice_key, "
                "legacy_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                (session, ref, state, now, 0 if fresh else 1, pid if fresh else None, start if fresh else None,
                 notice_key, 0 if fresh else 1))
        return "offer" if fresh else "skip"
    if row["delivered"]:
        return "skip"
    holder = row["holder_pid"]
    if _holder_is_me(holder) or _holder_alive(holder, row["holder_start"]):
        return "skip"
    if holder is None:
        return "skip"  # old silent delivered=0 record: never announced, do not flood
    key = notice_key or row["notice_key"] or ""
    if transcript_has is not None and transcript_has(key, task_id):
        with conn:
            conn.execute(
                "UPDATE notices SET delivered=1, holder_pid=NULL, holder_start=NULL, notice_key=? "
                "WHERE session=? AND ref=? AND state=? AND delivered=0",
                (key, session, ref, state))
        return "acked"
    with conn:
        cur = conn.execute(
            "UPDATE notices SET holder_pid=?, holder_start=?, notice_key=? "
            "WHERE session=? AND ref=? AND state=? AND delivered=0 AND holder_pid IS ?",
            (pid, start, key, session, ref, state, holder))
    return "offer" if cur.rowcount == 1 else "skip"


def collect(session_key: str, profile: str, *, hub_db: Optional[Path] = None, ledger: Optional[Path] = None,
            now: Optional[float] = None, lineage: tuple[str, ...] = (),
            skip: Optional[Callable[[str], bool]] = None,
            transcript_has: Optional[Callable[[str, str], bool]] = None) -> list[dict]:
    """Claim and return ``[{text, session, ref, state, event_key}]`` for this session's newly terminal remote cards.

    The ledger row stays in flight (holder pid + start) until :func:`ack`. ``lineage`` adds earlier ids of the
    same chat (a compaction fork keeps the link on the old id). ``skip(task_id)`` is true for remote cards a
    local placeholder already reports. ``transcript_has(notice_key, task_id)`` lets a dead holder's claim be
    acked instead of re-offered, and re-offers a legacy ``delivered=1`` row once. Never raises.
    """
    path = hub_db or hub_db_path()
    if not session_key or not path.is_file():
        return []
    now = time.time() if now is None else now
    profile = profile or "default"
    try:
        with _lock:
            me, index = _index(path)
        cards = []
        for sid in (session_key, *lineage):
            cards.extend(dict(c, sid=sid) for c in index.get((profile, sid), ()))
        if not cards:
            return []
        out: list[dict] = []
        conn = _ledger(ledger)
        try:
            with conn:
                out.extend(_reconcile_legacy(conn, session_key, cards, now, transcript_has, me, profile))
            offered = {(item["ref"], item["state"]) for item in out}
            for card in cards:
                if len(out) >= MAX_PER_POLL:
                    break
                task = card["task"]
                ref = f"{card['gw']}/{card['key']}"
                at = _terminal_at(task)
                state = f"{task['status']}@{int(at) if at else 0}"
                if (ref, state) in offered:
                    continue
                fresh = at is not None and now - at <= FRESH_S
                if fresh and skip is not None and conn.execute(
                        "SELECT 1 FROM notices WHERE session=? AND ref=? AND state=?",
                        (session_key, ref, state)).fetchone() is None and skip(str(task["id"])):
                    fresh = False  # recorded silently: the placeholder's own notice covers it
                key = _shared_key(card["gw"], card["key"], task, profile, card.get("sid") or session_key)
                verdict = _claim_notice(conn, session=session_key, ref=ref, state=state, now=now, fresh=fresh,
                                        notice_key=key, task_id=str(task["id"]), transcript_has=transcript_has)
                if verdict == "offer":
                    out.append(_notice_item(card, session_key, me, profile))
        finally:
            conn.close()
        return out
    except Exception:
        logger.debug("remote kanban collect failed", exc_info=True)
        return []


def ack(claim: dict, *, ledger: Optional[Path] = None) -> bool:
    """The notice is in the transcript: the in-flight row is delivered. Idempotent."""
    try:
        conn = _ledger(ledger)
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE notices SET delivered=1, holder_pid=NULL, holder_start=NULL "
                    "WHERE session=? AND ref=? AND state=? AND delivered=0",
                    (claim["session"], claim["ref"], claim["state"]))
                if cur.rowcount == 0 and claim.get("notice_key") and conn.execute(
                        "SELECT 1 FROM notices WHERE session=? AND ref=? AND state=?",
                        (claim["session"], claim["ref"], claim["state"])).fetchone() is None:
                    pid_at = time.time()
                    conn.execute(
                        "INSERT INTO notices (session, ref, state, at, delivered, notice_key, legacy_seen) "
                        "VALUES (?,?,?,?,1,?,1)",
                        (claim["session"], claim["ref"], claim["state"], pid_at, claim["notice_key"]))
            row = conn.execute(
                "SELECT delivered FROM notices WHERE session=? AND ref=? AND state=?",
                (claim["session"], claim["ref"], claim["state"])).fetchone()
            return row is not None and int(row["delivered"]) == 1
        finally:
            conn.close()
    except Exception:
        logger.debug("remote kanban ack failed", exc_info=True)
        return False


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


# [daveey] Notices already persisted (or found persisted), keyed (replica, profile, session, notice_id).
# Without it every 30 s pass re-ran append_kanban_notice for every done/blocked remote card: one state.db writer txn
# plus a lineage json scan each (441 per pass on the MacBook 2026-10-28, ~1 core of the Desktop backend, and write
# contention for live turns). The keys are also written to the ledger's ``swept`` table, so a restarted backend
# loads them instead of re-writing every notice once (443 opens of the 13 GB state.db, ~7 min and ~200 s CPU after
# each restart, 2026-10-29). A new event has a new notice_id; rows untouched for SWEPT_KEEP_S are pruned (a card
# still in the replica then costs one deduplicated append).
_DELIVERED_MAX = 50_000
SWEPT_KEEP_S = 90 * 86400.0
_delivered: dict[tuple, None] = {}
_swept_loaded: set[str] = set()


def _remember_delivered(key: tuple) -> None:
    _delivered[key] = None
    while len(_delivered) > _DELIVERED_MAX:
        _delivered.pop(next(iter(_delivered)))


def _swept(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS swept (replica TEXT NOT NULL, profile TEXT NOT NULL, session TEXT NOT NULL, "
                 "notice_id TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (replica, profile, session, notice_id))")


def _load_swept(ledger: Optional[Path]) -> None:
    """Once per process and ledger: prune old ``swept`` rows and remember the rest. Unreadable: in-memory only."""
    name = str(ledger or ledger_path())
    if name in _swept_loaded:
        return
    try:
        conn = _ledger(ledger)
        try:
            with conn:
                _swept(conn)
                conn.execute("DELETE FROM swept WHERE at < ?", (time.time() - SWEPT_KEEP_S,))
            for row in conn.execute("SELECT replica, profile, session, notice_id FROM swept ORDER BY at"):
                _remember_delivered(tuple(row))
        finally:
            conn.close()
        _swept_loaded.add(name)
    except sqlite3.Error:
        logger.debug("remote notice ledger unreadable; delivered notices kept in memory only", exc_info=True)


def _record_swept(keys: list[tuple], ledger: Optional[Path]) -> None:
    if not keys:
        return
    try:
        conn = _ledger(ledger)
        try:
            with conn:
                _swept(conn)
                now = time.time()
                conn.executemany("INSERT OR REPLACE INTO swept (replica, profile, session, notice_id, at) "
                                 "VALUES (?,?,?,?,?)", [(*key, now) for key in keys])
        finally:
            conn.close()
    except sqlite3.Error:
        logger.debug("remote notice ledger unwritable; %d delivered notices kept in memory only", len(keys),
                     exc_info=True)


def _reset_for_tests() -> None:
    """Forget this process's memos, as a restarted backend would (the ledger file stays)."""
    global _index_memo
    _index_memo = None
    _delivered.clear()
    _swept_loaded.clear()


logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _session_db(profile, sid):
    from hermes_cli.profiles import get_profile_dir
    from hermes_state import SessionDB

    path = get_profile_dir(profile) / "state.db"
    if not path.is_file():
        yield None, None
        return
    db = SessionDB(path)
    try:
        yield (db, db.get_compression_tip(sid) or sid) if db.get_session(sid) else (None, None)
    finally:
        db.close()


def sweep_remote_notices(*, replica_path=None, gateway=None, session_db=_session_db, live_ids=None,
                         ledger=None) -> list[dict]:
    """Persist addressed notices, then ack the ledger. Write-first: a failed append leaves no delivered row
    and the next sweep retries (no claim-before-durable). ``live_ids`` are open chats; their poller writes the
    row and starts the turn, so this sweep skips them. Done and blocked share one ``kanban_event_keys`` value
    with ``collect``, so running both yields one transcript row.
    """
    from hermes_cli.kanban_origin import _hub_db, gateway_names

    path = replica_path if replica_path is not None else _hub_db()
    if path is None:
        return []
    me = gateway if gateway is not None else gateway_names()[0]
    if not me:
        return []
    live = set(live_ids or ())
    try:
        with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            rows = conn.execute("SELECT gw,key,data FROM replica WHERE coll='tasks' AND gw!=?", (me,)).fetchall()
    except sqlite3.Error:
        logger.debug("remote Kanban replica unavailable", exc_info=True)
        return []
    _load_swept(ledger)
    delivered, swept = [], []
    try:
        _sweep_rows(rows, me, live, path, session_db, ledger, delivered, swept)
    finally:
        _record_swept(swept, ledger)
    return delivered


def _sweep_rows(rows, me, live, path, session_db, ledger, delivered: list, swept: list) -> None:
    for owner, key, raw in rows:
        try:
            task = json.loads(raw)
            notice = task.get("completion_notice")
            done = task.get("status") == "done" and isinstance(notice, dict) and "event_id" in notice
            blocked = task.get("status") == "blocked"
            if not done and not blocked:
                continue
            for link in task.get("sessions", []):
                if link.get("rel") != "shepherd" or link.get("gw") != me:
                    continue
                profile, sid = link.get("profile"), link.get("session")
                if not profile or not sid or sid in live:
                    continue
                if done:
                    event_id = notice["event_id"]
                    event_key = _event_key(owner, key, notice)
                    metadata = {"notice_id": f"remote:{owner}:{key}:{event_id}:{profile}:{sid}",
                                "board": task.get("board", "default"), "task_id": task["id"],
                                "gateway": owner, "event_ids": [event_id],
                                "kanban_event_keys": [event_key]}
                    text = _completion_text(owner, key, task, me, profile, sid)
                else:
                    event_key = _shared_key(owner, key, task, profile, sid)
                    metadata = {"notice_id": event_key, "board": task.get("board", "default"),
                                "task_id": task["id"], "gateway": owner, "kanban_event_keys": [event_key]}
                    text = format_notice(owner, key, task)
                memo_key = (str(path), profile, sid, metadata["notice_id"])
                if memo_key in _delivered:
                    continue
                with session_db(profile, sid) as (db, tip):
                    if db is not None and text:
                        row_id = db.append_kanban_notice(tip, text, metadata)
                        _remember_delivered(memo_key)
                        swept.append(memo_key)
                        delivered.append({"task_id": task["id"], "session_id": tip, "message_row": row_id})
                        at = _terminal_at(task)
                        ack({"session": sid, "ref": f"{owner}/{key}",
                             "state": f"{task['status']}@{int(at) if at else 0}",
                             "notice_key": event_key}, ledger=ledger)
        except Exception:
            logger.debug("remote Kanban notice deferred for %s/%s", owner, key, exc_info=True)
