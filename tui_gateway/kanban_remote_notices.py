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
(``release``) so the owner of the chat re-claims it. Freshness is the later of the owner's terminal time
and this host's durable first observation of that terminal state. The initial replica snapshot seeds old
states silently (and frozen legacy keys never get a local window), so a fresh host never floods history.
A state this host OBSERVED (``observations.first_seen`` set) that was never delivered inside ``FRESH_S`` (a busy
chat, a down backend) is still delivered once as a late notice until ``LATE_MAX_S`` after its first observation;
past that it is dropped with one WARNING, never silently.
A legacy
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
from hermes_cli.kanban_withdrawal_notice import envelope as _withdrawal
from tui_gateway.kanban_remote_notice_state import state as _notice_state

FRESH_S = 6 * 3600.0
# [daveey] How long, from this host's first observation, a state it OBSERVED (not one seeded by a source's first
# snapshot) is still delivered once it is past ``FRESH_S``, as a late notice. Past it the notice is dropped with one
# WARNING. 72 h spans a weekend with the Mac asleep after observing (hub card t_01a1c86d86cc1db7 was settled stale
# silently 6 h after it was first seen, while its chat stayed busy).
LATE_MAX_S = 72 * 3600.0
# ``notices.delivered``: 0 = in flight (or an old silent record), DELIVERED = the notice is in the chat transcript,
# SETTLED = recorded without a notice (stale, or a local placeholder reports it). Only a transcript write (``ack``)
# or a keyed transcript row (``_claim_notice``) sets DELIVERED; a SETTLED row also gets a ``swept`` row with the
# reason, so a ledger ack always means a transcript row. [daveey]
DELIVERED = 1
SETTLED = 2
MAX_PER_POLL = 10
LINK_RELS = ("shepherd",)
_TERMINAL = ("done", "blocked")

_lock = threading.Lock()
_index_memo: Optional[tuple] = None  # (stamp, file identity, signature, me, index)
_sweep_memo: Optional[tuple] = None  # (stamp, gateway, cards, current states)


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


def drop_mark(task: dict) -> Optional[float]:
    """``human_request.resolved_check_dropped_at`` of a blocked remote card (the owner's dropped-probe mark).

    A drop is a new state of the block, so it is the state's time: the dropped card gets its own ledger identity
    (``blocked@<dropped_at>``) and notifies each origin once more, even after the earlier block was delivered."""
    request = task.get("human_request")
    if isinstance(request, str):
        try:
            request = json.loads(request)
        except ValueError:
            return None
    if task.get("status") != "blocked" or not isinstance(request, dict):
        return None
    with contextlib.suppress(TypeError, ValueError):
        return float(request.get("resolved_check_dropped_at")) or None
    return None


def _terminal_at(task: dict) -> Optional[float]:
    if (dropped := drop_mark(task)) is not None:
        return dropped
    run = task.get("run") if isinstance(task.get("run"), dict) else {}
    for value in (task.get("completed_at") if task.get("status") == "done" else None, run.get("ended_at")):
        with contextlib.suppress(TypeError, ValueError):
            if value:
                return float(value)
    return None


def _fresh_at(task: dict) -> Optional[float]:
    """When the card reached its terminal state, for the freshness bound. Falls back to the owner's published
    ``completion_notice.created_at`` (records without ``completed_at``). Not part of the ledger identity."""
    withdrawal = _withdrawal(task)
    if withdrawal is not None:
        return float(withdrawal["created_at"])
    at = _terminal_at(task)
    if at is not None:
        return at
    notice = task.get("completion_notice")
    if isinstance(notice, dict) and notice.get("created_at"):
        with contextlib.suppress(TypeError, ValueError):
            return float(notice["created_at"])
    return None


# Payload pages of ``_task_rows``: at most ``_ROW_PAGE_MAX`` rows and about ``_ROW_PAGE_BYTES`` of row text at the
# replica's mean row size. One ``json_group_array`` over every payload was a 256 MB string (UCS-4 once any row holds
# an emoji) plus its decode: a ~450 MB peak per cold read on the mini's 17k-row replica. [daveey]
_ROW_PAGE_MAX = 4000
_ROW_PAGE_BYTES = 4 << 20


def _task_rows(conn: sqlite3.Connection, me: Optional[str] = None) -> list[tuple]:
    """``(gw, key, seq, data)`` task rows of the replica, other gateways only when ``me`` is given.

    Identities come from one query in the planner's order (as before); payloads from rowid pages, all in one read
    snapshot (the caller's transaction, else one opened here), so neither crosses the SQLite/Python boundary per
    task (GIL reacquisition) nor builds one string of every payload. [daveey]"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(replica)")}
    seq = "seq" if "seq" in cols else "0"
    # Keep data as text: json(data) would let one malformed task poison the entire read.
    where, args = ("coll = 'tasks' AND gw != ?", (me,)) if me else ("coll = 'tasks'", ())
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        packed, count, octets = conn.execute(
            f"SELECT json_group_array(json_array(rowid, gw, key, {seq})), count(*), total(octet_length(data)) "
            f"FROM replica WHERE {where}", args).fetchone()
        ids = json.loads(packed)
        size = max(50, min(_ROW_PAGE_MAX, int(_ROW_PAGE_BYTES // max(float(octets or 0) / max(count, 1), 1.0))))
        data: dict = {}
        after = -1 << 63
        while len(data) < count:
            page, last = conn.execute(
                "SELECT json_group_array(json_array(rowid, data)), max(rowid) FROM (SELECT rowid, data FROM replica"
                f" WHERE {where} AND rowid > ? ORDER BY rowid LIMIT ?)", (*args, after, size)).fetchone()
            if last is None:
                break
            data.update(json.loads(page))
            del page
            after = last
    finally:
        if own:
            conn.rollback()
    return [(gw, key, s, data.get(rowid)) for rowid, gw, key, s in ids]


def _canonical(rows: list[tuple]) -> list[tuple]:
    """Drop replica rows a newer key form supersedes. [daveey]

    The owner's hermes-hub client once published tasks as ``<board>/<id>``; it now publishes
    ``<profile>/<board>/<id>``. The replica keeps the old rows frozen in whatever status they had then
    (2026-10-29 MacBook: 252 hub rows, e.g. ``default/t_cc5aeb02`` still ``blocked`` beside a ``done``
    ``default/default/t_cc5aeb02``), so reading every row replays states the card left days ago. A legacy row
    is dropped when the same gateway has any current row for that board and id; current rows are all kept
    (two profiles' cards may share a board name and an id).
    """
    current = {(str(row[0]), "/".join(str(row[1]).split("/")[-2:])) for row in rows if str(row[1]).count("/") >= 2}
    return [row for row in rows if str(row[1]).count("/") >= 2 or (str(row[0]), str(row[1])) not in current]


def _index(path: Path) -> tuple[str, dict]:
    """``(this gateway, {(profile, session): [card, ...]})`` for terminal cards owned by other gateways, cached on
    the replica observer's change key (an unchanged replica is not read). Per-row routing projections
    survive unrelated writes and are shared by every chat's poll."""
    global _index_memo
    from tui_gateway.kanban_replica_observer import file_identity, fingerprint

    stamp = fingerprint(path)
    memo = _index_memo  # (stamp, file identity, sig, me, index), kept only from reads the key brackets
    if stamp is not None and memo is not None and memo[0] == stamp:
        return memo[3], memo[4]
    _index_memo = None
    conn = _ro(path)
    try:
        row = conn.execute("SELECT v FROM meta WHERE k = 'gateway'").fetchone()
        me = str(row[0]) if row and row[0] else ""
        from tui_gateway.remote_index_projection import snapshot
        index = snapshot(conn, file_identity(stamp) if stamp is not None else None, me)
        stable = stamp is not None and fingerprint(path) == stamp
    finally:
        conn.close()
    if stable:
        _index_memo = (stamp, file_identity(stamp), None, me, index)
    return me, index


def _chat_index(path: Path, ledger, now: float, profile: str, sids: tuple) -> tuple[str, dict[str, Any], dict]:
    """``(me, {sid: [card, ...]}, observed)`` for one chat: the host's published copy when it is of the replica's
    current state, else built here (``_index`` + ``observe``, as before) and offered for publishing. [daveey]"""
    from tui_gateway import kanban_remote_index as published
    from tui_gateway.kanban_replica_observer import fingerprint

    ledger_file = Path(ledger) if ledger is not None else ledger_path()
    stamp = fingerprint(path)
    hit = published.lookup(path, ledger_file, stamp, profile, sids)
    if hit is not None:
        return hit
    me, index, observed = published.compute(path, ledger, ledger_file, now,
                                            published.current_key(path, ledger_file, stamp))
    return me, {sid: index.get((profile, sid), ()) for sid in sids}, observed


def _linked_chats(task: dict, me: str) -> list[tuple[str, str]]:
    """``(profile, session)`` of this gateway's chats a remote card notifies: its unreleased shepherd links, and
    the origin chat of every origin binding (``kanban_origin_adopt``) naming this gateway. [daveey]

    An adopted card carries its origin only as an ``origin_bindings`` marker; the owner publishes no shepherd link
    for it unless a hub subscription happens to exist. Reading only ``sessions`` left that origin chat with no
    done notice at all, so it never summarised the finished work and its outcome stayed ``unresolved{no_summary}``
    (hub card t_a989e5e0 -> MacBook chat 20261026_110445_20914b, 2026-10-31: the notice reached five unrelated
    shepherd transcripts and not the chat that asked for the work)."""
    if _withdrawal(task) is not None and "withdrawal_recipients" in task:
        # Archive captures eligibility before cleanup releases links/removes subs.
        # Never recover a destination from the mutable current binding projection.
        recipients = task["withdrawal_recipients"]
        return list(dict.fromkeys(
            (link["profile"], link["session"])
            for link in recipients if isinstance(link, dict) and link.get("gw") == me
            and all(isinstance(link.get(key), str) and link[key] for key in ("profile", "session"))
            and not _released(link)
        )) if isinstance(recipients, list) else []
    out: list[tuple[str, str]] = []
    links = task.get("sessions")
    released = {(str(link.get("profile") or "default"), str(link["session"]))
                for link in links if isinstance(link, dict) and link.get("gw") == me
                and link.get("session") and _released(link)} if isinstance(links, list) else set()
    for link in links if isinstance(links, list) else ():
        if (isinstance(link, dict) and link.get("gw") == me and link.get("rel") in LINK_RELS
                and link.get("session") and not _released(link)):
            out.append((str(link.get("profile") or "default"), str(link["session"])))
    bindings = task.get("origin_bindings")
    for binding in bindings if isinstance(bindings, list) else ():
        origin = binding.get("origin") if isinstance(binding, dict) else None
        if isinstance(origin, dict) and origin.get("gateway") == me and origin.get("session"):
            out.append((str(origin.get("profile") or "default"), str(origin["session"])))
    return [chat for chat in dict.fromkeys(out) if chat not in released]


def _released(link: dict) -> bool:
    """[daveey] The owner released this chat's link: its notices stop (session-outcome fan-out filter)."""
    from tui_gateway.notice_fanout import link_released
    return link_released(link)


def _wal(conn: sqlite3.Connection, deadline: float) -> None:
    """[daveey] ``journal_mode=WAL`` answers SQLITE_BUSY without calling the busy handler while another opener of a
    fresh ledger holds it, so concurrent first polls raised ``database is locked``. Retry until the connect timeout."""
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def _ledger(path: Optional[Path] = None) -> sqlite3.Connection:
    path = path or ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    _wal(conn, deadline=time.monotonic() + 5.0)
    conn.execute("CREATE TABLE IF NOT EXISTS notices (session TEXT NOT NULL, ref TEXT NOT NULL, state TEXT NOT NULL, "
                 "at REAL NOT NULL, delivered INTEGER NOT NULL, PRIMARY KEY (session, ref, state))")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(notices)")}
    for name, decl in (("holder_pid", "INTEGER"), ("holder_start", "INTEGER"), ("notice_key", "TEXT"),
                       ("legacy_seen", "INTEGER NOT NULL DEFAULT 0"), ("settled", "TEXT")):
        if name not in cols:
            try:
                conn.execute(f"ALTER TABLE notices ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError as exc:  # [daveey] a concurrent opener added it first
                if "duplicate column name" not in str(exc):
                    raise
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
    if _withdrawal(task) is not None:
        from tui_gateway import server
        return server._format_kanban_event_text(
            {"task_id": tid}, SimpleNamespace(**task),
            SimpleNamespace(kind="request_withdrawn", payload={}), board)
    if task.get("status") == "done":
        handoff = _first_line(run.get("summary") or task.get("result"), 200)
        return f"✔ {prefix}Kanban {tid} done — {title}{handoff}\n(ran on {gw}; read it with cross_task @task:{board}@{gw}/{tid})"
    reason = run.get("summary") or task.get("block_kind") or ""
    from hermes_cli.kanban_block_notice import block_notice
    return f"⏸ {prefix}Kanban {tid} " + block_notice(task, tid, board, gateway=gw, reason=str(reason), actor=str(task.get("block_actor") or ""))


def _event_key(owner: str, key: str, notice: dict) -> str:
    """Event identity shared by the poller (``collect``) and the sweeper, so a chat gets one notice per event."""
    return f"remote:{owner}:{key}:{notice['event_id']}"


def _notice_id(owner: str, key: str, task: dict, profile: str, sid: str) -> str:
    """The sweeper's per-chat ``notice_id`` (its ``swept`` and memo identity) for this card state."""
    withdrawal = _withdrawal(task)
    if withdrawal is not None:
        return f"{_event_key(owner, key, withdrawal)}:{profile}:{sid}"
    notice = task.get("completion_notice")
    if task.get("status") == "done" and isinstance(notice, dict) and "event_id" in notice:
        return f"remote:{owner}:{key}:{notice['event_id']}:{profile}:{sid}"
    return _shared_key(owner, key, task, profile, sid)


def _shared_key(owner: str, key: str, task: dict, profile: str, sid: str) -> str:
    """The ``kanban_event_keys`` value both paths record. Done uses :func:`_event_key`; blocked has no
    completion event, so the terminal state plus the destination is the identity."""
    withdrawal = _withdrawal(task)
    if withdrawal is not None:
        return _event_key(owner, key, withdrawal)
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


# [daveey] Claims this process holds for a woken turn whose row was not yet durable at admission
# (``_settle_started``): ``{(session, ref, state): live session dict}``. The dict is the woken turn's own record,
# held by reference (a compaction may re-key the chat mid-turn); its ``running`` flag is set at admission
# (``_notif_claim_turn``) and cleared when the turn ends, row or not. In-process only: a dead holder is the
# successor's dead-holder path. Without it the claim waited for this process to exit when the turn ended with no
# row (t_01a18c287684accd). No wall-clock timeout: a slow turn that writes the row late is never duplicated.
_woken: dict[tuple, dict] = {}
_woken_lock = threading.Lock()


def _track_woken(claim_id: tuple, session: dict) -> None:
    with _woken_lock:
        _woken[claim_id] = session


def _woken_turn_ended(claim_id: tuple) -> bool:
    """True when this process woke a turn for ``claim_id`` and that chat has no running turn now."""
    with _woken_lock:
        session = _woken.get(claim_id)
    return session is not None and not session.get("running")


def _forget_woken(claim_id: tuple) -> bool:
    """Drop the tracked wake; True for the one caller that removed it (so a claim is re-offered once)."""
    with _woken_lock:
        return _woken.pop(claim_id, None) is not None


def _live_woken_session(*keys: str) -> Optional[dict]:
    """The live session dict this process holds for the woken chat (any of ``keys``), else None."""
    try:
        from tui_gateway import server
        for key in keys:
            live = server._find_live_session_by_key(key) if key else None
            if live is not None:
                return live[1]
    except Exception:
        logger.debug("remote kanban: live session lookup for a woken chat failed", exc_info=True)
    return None


def notice_in_transcript(db, session_id: str, notice_key: str, task_id: str) -> bool:
    """Delivery evidence in this lineage: a keyed row, completed persisted automation receipt,
    or legacy ``Kanban <id>`` line. The historical name is retained for collector callbacks."""
    if db is None:
        return False
    from tui_gateway.kanban_remote_receipts import completed_notice
    if completed_notice(db, session_id, notice_key):
        return True
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
    from tui_gateway import server
    sub = {"task_id": task["id"], "chat_id": f"{me}/{profile}/{sid}"}
    ev = SimpleNamespace(kind="completed", payload=notice.get("payload"))
    return server._format_kanban_event_text(sub, SimpleNamespace(**task), ev, task.get("board", "default"))


def remote_blocked_on_human(task: dict) -> bool:
    """Whether a remote card is parked on a validated human request, from its replicated task record. Stored in
    the notice's metadata: the notice guarantee cannot look the card up on this gateway. [daveey]"""
    try:
        from types import SimpleNamespace

        from tui_gateway.kanban_unloaded import _blocked_on_human
        return bool(_blocked_on_human(SimpleNamespace(status=task.get("status"), block_kind=task.get("block_kind"),
                                                      human_request=task.get("human_request"))))
    except Exception:
        logger.debug("remote blocked_on_human check failed", exc_info=True)
        return False


def _notice_item(card: dict, session_key: str, me: str, profile: str, now: Optional[float] = None) -> dict:
    task = card["task"]
    sid = card.get("sid") or session_key
    text = _completion_text(card["gw"], card["key"], task, me, profile, sid)
    item = {"text": text or format_notice(card["gw"], card["key"], task), "session": session_key,
            "ref": f"{card['gw']}/{card['key']}", "state": _notice_state(task),
            "blocked_on_human": remote_blocked_on_human(task)}
    if text:
        item["event_key"] = _event_key(card["gw"], card["key"], task["completion_notice"])
    elif task.get("status") == "blocked" or _withdrawal(task) is not None:
        item["event_key"] = _shared_key(card["gw"], card["key"], task, profile, sid)
    if now is not None:
        from tui_gateway.kanban_remote_late import owner_time
        item["owner_at"] = owner_time(task, now)
    item.setdefault("event_key", _shared_key(card["gw"], card["key"], task, profile, sid))
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
        by_state[(f"{card['gw']}/{card['key']}", _notice_state(task))] = card
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
            out.append(_notice_item(card, session_key, me, profile, now))
    if stale:
        logger.info("remote kanban: left %d delivered notice(s) unreoffered "
                    "(older than %.0fh, or not in the replica)", stale, FRESH_S / 3600)
    if out:
        logger.info("remote kanban: re-offered %d legacy notice(s) claimed delivered but absent "
                    "from the transcript", len(out))
    return out


class _ExpiredReason:
    """The settle reason of an observed state past ``LATE_MAX_S``: ``transcript`` when the chat already has its row
    (under another session id of it: nothing was lost), else ``expired``. Resolved lazily, by ``_claim_notice``,
    only when the ledger has no row for it yet: every pass offers each expired card again, and one lineage scan
    per card per pass cost the gateway's remote owner pass 12-20 s on two chats with 158 and 207 already-settled
    expired cards (t_01a1d4de28a8510d). ``reason`` stays None until resolved. [daveey]"""

    def __init__(self, transcript_has: Callable[[str, str], bool], key: str) -> None:
        self.transcript_has, self.key = transcript_has, key
        self.reason: Optional[str] = None

    def __call__(self) -> str:
        self.reason = "transcript" if self.transcript_has(self.key, "") else "expired"
        return self.reason


def _claim_notice(conn, *, session, ref, state, now, fresh, notice_key, task_id, transcript_has,
                  swept_key: Optional[tuple] = None,
                  settle_reason: str | _ExpiredReason = "stale") -> str:
    """``offer`` a new or orphaned claim, ``acked`` when the transcript already has it, else ``skip``.

    A fresh claim is in flight (delivered=0, this process's pid + start). It becomes delivered only in
    :func:`ack`, after the transcript write. A holder that is gone is re-offered unless the key is already
    in the transcript. A state that is not fresh is recorded SETTLED (never DELIVERED, ``settled`` =
    ``settle_reason``) with a ``swept`` row carrying ``settle_reason`` when ``swept_key`` is given; that first
    settle returns ``settled``. An :class:`_ExpiredReason` is resolved only for that first settle (no row yet):
    it may cost a transcript scan, which a row already in the ledger never needs.

    [daveey] The dead-holder check matches the claim's own key only. It used ``notice_in_transcript``'s legacy
    ``Kanban <task id>`` text match too, so ANY earlier notice of the same card (an earlier blocked state)
    proved this new state delivered: hub card t_b49c4888 blocked again at 22:01 PDT 2026-10-29, the claiming
    Desktop backend was replaced by the 22:06 core update, and its successor acked ledger row 1245 against the
    16:03 notice of the previous block. The chat never saw the new block.
    """
    row = conn.execute(
        "SELECT delivered, holder_pid, holder_start, notice_key FROM notices WHERE session=? AND ref=? AND state=?",
        (session, ref, state)).fetchone()
    pid, start = _my_holder()
    if row is None:
        if not fresh and isinstance(settle_reason, _ExpiredReason):
            settle_reason = settle_reason()  # outside the write txn: it may scan the transcript
        reason = str(settle_reason)
        with conn:
            conn.execute(
                "INSERT INTO notices (session, ref, state, at, delivered, holder_pid, holder_start, notice_key, "
                "legacy_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                (session, ref, state, now, 0 if fresh else SETTLED, pid if fresh else None,
                 start if fresh else None, notice_key, 0 if fresh else 1))
            if not fresh:
                conn.execute("UPDATE notices SET settled=? WHERE session=? AND ref=? AND state=?",
                             (reason, session, ref, state))
                _swept_row(conn, swept_key, reason)
        return "offer" if fresh else "settled"
    if row["delivered"]:
        return "skip"
    holder = row["holder_pid"]
    if _holder_is_me(holder):
        # [daveey] This process's own claim: a woken turn was admitted and its row is written later, on the
        # turn thread (``_settle_started`` leaves the claim in flight until then). Ack it once its own key is
        # in the transcript; until then it stays in flight, and a death re-offers it below in the successor.
        # A woken turn that already ENDED without the row (context refused, an error before the persist) is
        # re-offered here, once: its running flag is read BEFORE the transcript check, so a row the turn wrote
        # before it ended is always seen and acked instead.
        key = notice_key or row["notice_key"] or ""
        ended = _woken_turn_ended((session, ref, state))
        if key and transcript_has is not None and transcript_has(key, ""):
            with conn:
                cur = conn.execute(
                    "UPDATE notices SET delivered=?, holder_pid=NULL, holder_start=NULL, notice_key=? "
                    "WHERE session=? AND ref=? AND state=? AND delivered=0 AND holder_pid IS ?",
                    (DELIVERED, key, session, ref, state, holder))
                if cur.rowcount == 1:
                    _swept_row(conn, swept_key, "delivered")
            if cur.rowcount == 1:
                _forget_woken((session, ref, state))
                logger.info("remote kanban: started turn's notice %s %s for chat %s is in the transcript; acked",
                            ref, state, session)
                return "acked"
        if ended and key and transcript_has is not None and _forget_woken((session, ref, state)):
            logger.info("remote kanban: woken turn for notice %s %s in chat %s ended without its row; "
                        "re-offering", ref, state, session)
            return "offer"
        return "skip"
    if _holder_alive(holder, row["holder_start"]):
        return "skip"
    if holder is None:
        return "skip"  # old silent delivered=0 record: never announced, do not flood
    key = notice_key or row["notice_key"] or ""
    # A keyed claim is proven only by its own key; the task-id text match is for unkeyed (legacy) rows.
    if transcript_has is not None and transcript_has(key, "" if key else task_id):
        with conn:
            conn.execute(
                "UPDATE notices SET delivered=?, holder_pid=NULL, holder_start=NULL, notice_key=? "
                "WHERE session=? AND ref=? AND state=? AND delivered=0",
                (DELIVERED, key, session, ref, state))
            _swept_row(conn, swept_key, "transcript")
        logger.info("remote kanban: dead holder pid %s's claim %s %s for chat %s is already in the transcript; "
                    "acked", holder, ref, state, session)
        return "acked"
    with conn:
        cur = conn.execute(
            "UPDATE notices SET holder_pid=?, holder_start=?, notice_key=? "
            "WHERE session=? AND ref=? AND state=? AND delivered=0 AND holder_pid IS ?",
            (pid, start, key, session, ref, state, holder))
    return "offer" if cur.rowcount == 1 else "skip"


def _observed_first_seen(fresh_times, ident: tuple) -> Optional[float]:
    from tui_gateway.kanban_remote_observations import first_seen_of
    return first_seen_of(fresh_times, ident)


def _late_verdict(now: float, first_seen: float) -> str:
    """``late`` while an observed state is inside ``LATE_MAX_S`` of its first observation, else ``expired``."""
    return "late" if now - first_seen <= LATE_MAX_S else "expired"


def _log_dropped(task: dict, ref: str, state: str, session: str, first_seen: float, now: float) -> None:
    """The one WARNING for an observed notice that is settled without ever reaching its chat."""
    from datetime import UTC, datetime
    minutes = int((now - first_seen) // 60)
    logger.warning("remote kanban: dropped undelivered notice for card %s (%s %s) to chat %s: first seen %s, "
                   "age %dh%02dm, past the %.0fh late window", task.get("id"), ref, state, session,
                   datetime.fromtimestamp(first_seen, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   minutes // 60, minutes % 60, LATE_MAX_S / 3600)


def collect(session_key: str, profile: str, *, hub_db: Optional[Path] = None, ledger: Optional[Path] = None,
            now: Optional[float] = None, lineage: tuple[str, ...] = (),
            skip: Optional[Callable[[str], bool]] = None,
            transcript_has: Optional[Callable[[str, str], bool]] = None, per_link: bool = False) -> list[dict]:
    """Claim and return ``[{text, session, ref, state, event_key}]`` for this session's newly terminal remote cards.

    The ledger row stays in flight (holder pid + start) until :func:`ack`. ``lineage`` adds earlier ids of the
    same chat (a compaction fork keeps the link on the old id). ``skip(task_id)`` is true for remote cards a
    local placeholder already reports. ``transcript_has(notice_key, task_id)`` lets a dead holder's claim be
    acked instead of re-offered, and re-offers a legacy ``delivered=1`` row once. ``per_link`` claims each
    notice under its link's own session, the ledger key the Desktop sweep (``_deliver_link``) claims it
    under, so the two see each other's claim on a chat compressed before the card was linked
    (``gateway.kanban_remote_owner``; review t_01a1d013e0411032). Never raises.
    """
    from hermes_cli.kanban_placement import is_placeholder_session
    if is_placeholder_session(session_key):
        return []
    path = hub_db or hub_db_path()
    if not session_key or not path.is_file():
        return []
    now = time.time() if now is None else now
    profile = profile or "default"
    try:
        sids = (session_key, *lineage)
        if hub_db is None and ledger is None:  # the host's own replica: keep its published copy current
            from tui_gateway.kanban_remote_index import ensure_holder
            ensure_holder("serve")  # once per process; a gateway's own later claim upgrades the role
        me, by_sid, fresh_times = _chat_index(path, ledger, now, profile, sids)
        cards = [dict(c, sid=sid) for sid in sids for c in by_sid.get(sid, ())]
        if not cards:
            return []
        out: list[dict] = []
        conn = _ledger(ledger)
        try:
            with conn:
                out.extend(_reconcile_legacy(conn, session_key, cards, now, transcript_has, me, profile))
            offered = {(item["ref"], item["state"]) for item in out}
            for card in cards:
                from tui_gateway.kanban_remote_late import owner_time
                if owner_time(card["task"], now) is None and sum(
                        item.get("owner_at") is None for item in out) >= MAX_PER_POLL:
                    continue
                task = card["task"]
                ref = f"{card['gw']}/{card['key']}"
                state = _notice_state(task)
                if (ref, state) in offered:
                    continue
                ident = (card["gw"], card["key"], state)
                at_fresh = fresh_times.get(ident, _fresh_at(task))
                fresh = at_fresh is not None and now - at_fresh <= FRESH_S
                if not fresh and ident not in fresh_times:
                    continue  # partial sync: don't permanently settle a not-yet-observed arrival
                sid = card.get("sid") or session_key
                claim_session = sid if per_link else session_key
                key = _shared_key(card["gw"], card["key"], task, profile, sid)
                # [daveey] A state this host observed while fresh and never delivered (a busy chat, a down backend)
                # is still claimed past FRESH_S, as a late notice (its owner time is old, so ``_notice_item`` puts
                # it in the late group). Only a seeded state (first snapshot) settles silently as ``stale``.
                first_seen = _observed_first_seen(fresh_times, ident)
                reason = "stale" if fresh or first_seen is None else _late_verdict(now, first_seen)
                settle: str | _ExpiredReason = reason
                if reason == "late":
                    fresh = True
                elif reason == "expired" and transcript_has is not None:
                    settle = _ExpiredReason(transcript_has, key)
                if fresh and skip is not None and conn.execute(
                        "SELECT 1 FROM notices WHERE session=? AND ref=? AND state=?",
                        (claim_session, ref, state)).fetchone() is None and skip(str(task["id"])):
                    fresh, settle = False, "placeholder"  # recorded silently: the placeholder's notice covers it
                swept_key = (str(path), profile, sid, _notice_id(card["gw"], card["key"], task, profile, sid))
                verdict = _claim_notice(conn, session=claim_session, ref=ref, state=state, now=now, fresh=fresh,
                                        notice_key=key, task_id=str(task["id"]), transcript_has=transcript_has,
                                        swept_key=swept_key, settle_reason=settle)
                if verdict == "settled" and getattr(settle, "reason", settle) == "expired":
                    _log_dropped(task, ref, state, claim_session, first_seen or 0.0, now)  # expired: observed
                if verdict == "offer":
                    out.append(_notice_item(card, claim_session, me, profile, now))
                    offered.add((ref, state))
                    _note_poller_claim(sid, claim_session, ref, state)  # after the claim is out; never raises
        finally:
            conn.close()
        from tui_gateway.kanban_remote_late import collect_groups, release_undelivered
        try:
            return collect_groups(out, now)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            logger.warning("remote kanban: grouping late notices failed; releasing %d claim(s)", len(out),
                           exc_info=True)
            release_undelivered(out, ledger)
            return []
    except Exception:
        logger.debug("remote kanban collect failed", exc_info=True)
        return []


def ack(claim: dict, *, ledger: Optional[Path] = None) -> bool:
    """The notice is in the transcript: the in-flight row is delivered. Idempotent."""
    if claim.get("children"):
        results = [ack(child, ledger=ledger) for child in claim["children"]]
        return all(results)
    _forget_woken((claim.get("session"), claim.get("ref"), claim.get("state")))
    try:
        conn = _ledger(ledger)
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE notices SET delivered=?, holder_pid=NULL, holder_start=NULL "
                    "WHERE session=? AND ref=? AND state=? AND delivered=0",
                    (DELIVERED, claim["session"], claim["ref"], claim["state"]))
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
            return row is not None and int(row["delivered"]) == DELIVERED
        finally:
            conn.close()
    except Exception:
        logger.debug("remote kanban ack failed", exc_info=True)
        return False


def release(claim: dict, *, ledger: Optional[Path] = None) -> bool:
    """Hand a refused notice back so the chat's owning backend claims it on its next poll."""
    if claim.get("children"):
        results = [release(child, ledger=ledger) for child in claim["children"]]
        return all(results)
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
_last_stale: dict[str, int] = {}  # replica -> stale count last logged, so the sweep logs a change, not every pass
# [daveey] Why each notice last waited, per (chat, ref, state). The sweep's silent skips (chat open in this backend,
# claim in flight under another backend, left to the gateway's FIFO) and the live poller's claim each log once per
# change of reason. Without it a notice held in flight for 30 min left no line, and the 46 min "discovery lag" on
# t_01a1d46be379dfd3 was filed against a poller that had seen the card in 63 s.
_waits: dict[tuple, str] = {}
_waits_lock = threading.Lock()
_WAITS_MAX = 5000
_POLLER_CLAIM = "claimed by the live poller of backend pid "
_poller_claims: dict[tuple, str] = {}  # (link session, ref, state) -> session the poller's ledger row is keyed by
_CLAIM_DELIVERED = "\0delivered"  # _poller_claims value once the row read back delivered


def _note_wait(session: str, ref: str, state: str, why: str) -> None:
    """Log why a notice waits, once per change of reason. Never raises: collect calls it holding a claim, and an
    exception there (eviction racing a concurrent sweep) would strand that claim in flight (PR #2116 review)."""
    try:
        key = (session, ref, state)
        with _waits_lock:
            if _waits.get(key) == why:
                return
            _waits.pop(key, None)
            _waits[key] = why
            while len(_waits) > _WAITS_MAX:
                _waits.pop(next(iter(_waits)), None)
        logger.info("remote Kanban notice %s %s for chat %s waits: %s", ref, state, session, why)
    except Exception:  # diagnostics only; the caller's claim must not depend on them
        with contextlib.suppress(Exception):
            logger.debug("remote kanban: wait log failed", exc_info=True)


def _note_poller_claim(link_sid: str, claim_session: str, ref: str, state: str) -> None:
    """Log the live poller's claim and remember which ledger row holds it, keyed by the LINK's session: the sweep
    looks it up by the link, while the claim may sit under the live session key of a compacted chat (PR #2158
    review). Never raises."""
    try:
        with _waits_lock:
            _poller_claims.pop((link_sid, ref, state), None)
            _poller_claims[(link_sid, ref, state)] = claim_session
            while len(_poller_claims) > _WAITS_MAX:
                _poller_claims.pop(next(iter(_poller_claims)), None)
    except Exception:  # diagnostics only
        with contextlib.suppress(Exception):
            logger.debug("remote kanban: poller claim marker failed", exc_info=True)
    _note_wait(claim_session, ref, state, f"{_POLLER_CLAIM}{os.getpid()}")


def _poller_claim_state(claim_session: str, ref: str, state: str, ledger) -> str:
    """``delivered`` (final), ``held`` (in flight under a live backend, or the ledger is unreadable: this only
    picks a log level) or ``stuck``: released undelivered (``release_undelivered``) or held by a dead pid."""
    try:
        with contextlib.closing(_ledger(ledger)) as conn:
            row = conn.execute("SELECT delivered, holder_pid, holder_start FROM notices "
                               "WHERE session=? AND ref=? AND state=?", (claim_session, ref, state)).fetchone()
    except Exception:
        logger.debug("remote kanban: poller claim readback failed", exc_info=True)
        return "held"
    if row is None:
        return "stuck"
    if row["delivered"]:
        return "delivered"
    return "held" if _holder_alive(row["holder_pid"], row["holder_start"]) else "stuck"


def _note_open_chat(sid: str, ref: str, state: str, waker, observed_at, now, ledger=None) -> None:
    """The healthy path: this backend's live poller claims the notice within seconds and logs that claim. Only a
    notice past the waker's grace window whose poller claim is missing, released undelivered or held by a dead
    backend logs the open-chat skip at INFO (PR #2116, #2158 reviews)."""
    grace = waker.grace_s if waker is not None else 0.0
    if observed_at is not None and now is not None and now - observed_at >= grace:
        with _waits_lock:
            claim_session = _poller_claims.get((sid, ref, state))
        if claim_session is None:
            verdict = "stuck"
        elif claim_session == _CLAIM_DELIVERED:
            verdict = "delivered"
        else:
            verdict = _poller_claim_state(claim_session, ref, state, ledger)
        if verdict == "stuck":
            _note_wait(sid, ref, state, "chat is open in this backend; its live poller claims it")
            return
        if verdict == "delivered" and claim_session != _CLAIM_DELIVERED:
            with _waits_lock:  # final: later passes skip the ledger read
                if _poller_claims.get((sid, ref, state)) == claim_session:
                    _poller_claims[(sid, ref, state)] = _CLAIM_DELIVERED
    logger.debug("remote Kanban notice %s %s for chat %s: chat is open in this backend", ref, state, sid)


def _wait_reason(conn, session: str, ref: str, state: str) -> Optional[str]:
    """Why ``_claim_notice`` skipped an undelivered notice: the backend whose claim holds it, else None."""
    row = conn.execute("SELECT delivered, holder_pid FROM notices WHERE session=? AND ref=? AND state=?",
                       (session, ref, state)).fetchone()
    if row is None or row["delivered"] or row["holder_pid"] is None:
        return None
    if _holder_is_me(row["holder_pid"]):
        return f"in flight in this backend (pid {row['holder_pid']}) until its turn's row is written"
    return f"in flight under backend pid {row['holder_pid']} until that claim is acked or released"


def _remember_delivered(key: tuple) -> None:
    _delivered[key] = None
    while len(_delivered) > _DELIVERED_MAX:
        _delivered.pop(next(iter(_delivered)))


def _swept(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS swept (replica TEXT NOT NULL, profile TEXT NOT NULL, session TEXT NOT NULL, "
                 "notice_id TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (replica, profile, session, notice_id))")
    if "reason" not in {row[1] for row in conn.execute("PRAGMA table_info(swept)")}:
        # [daveey] why the notice is settled: delivered (row written / turn started), transcript (a dead holder's
        # claim found by its key), stale or placeholder (recorded without a notice: ``notices.delivered=SETTLED``)
        conn.execute("ALTER TABLE swept ADD COLUMN reason TEXT")


def _swept_row(conn: sqlite3.Connection, swept_key: Optional[tuple], reason: str) -> None:
    """Record one settled notice in ``swept`` inside the caller's ledger transaction, and in this process's memo."""
    if not swept_key:
        return
    _swept(conn)
    conn.execute("INSERT OR REPLACE INTO swept (replica, profile, session, notice_id, at, reason) VALUES (?,?,?,?,?,?)",
                 (*swept_key, time.time(), reason))
    _remember_delivered(tuple(swept_key))


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


def _record_swept(keys: list[tuple], ledger: Optional[Path], *, reason: str) -> None:
    """[daveey] ``reason`` has no default: a settle that wrote no notice must say so (stale/placeholder), never
    inherit ``delivered``."""
    if not keys:
        return
    try:
        conn = _ledger(ledger)
        try:
            with conn:
                _swept(conn)
                now = time.time()
                conn.executemany("INSERT OR REPLACE INTO swept (replica, profile, session, notice_id, at, reason) "
                                 "VALUES (?,?,?,?,?,?)", [(*key, now, reason) for key in keys])
        finally:
            conn.close()
    except sqlite3.Error:
        logger.debug("remote notice ledger unwritable; %d delivered notices kept in memory only", len(keys),
                     exc_info=True)


def _reset_for_tests() -> None:
    """Forget this process's memos, as a restarted backend would (the ledger file stays)."""
    from tui_gateway.kanban_remote_observations import _generation, _known, _memo, _pending
    from tui_gateway.kanban_remote_observations import _flights, _retained
    from tui_gateway.kanban_replica_observer import reset as reset_observers
    from tui_gateway.remote_sweep_projection import reset as reset_projection

    from tui_gateway.remote_index_projection import reset as reset_index_projection
    from tui_gateway.kanban_remote_index import reset as reset_published_index
    reset_published_index()
    reset_projection()
    reset_index_projection()
    global _index_memo, _sweep_memo
    _sweep_memo = None
    reset_observers()
    _memo.clear()
    _known.clear()
    _generation.clear()
    _pending.clear()  # never orphans a waiter: its leader sets ``done`` whether or not it is still listed
    _flights.clear()
    _retained.clear()
    _index_memo = None
    _delivered.clear()
    _swept_loaded.clear()
    _last_stale.clear()
    _waits.clear()
    _poller_claims.clear()
    with _woken_lock:
        _woken.clear()


logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _session_db(profile, sid):
    """Reads on the kept read-only store; write a notice through ``notice_writer(db)`` (card I). [daveey]"""
    from hermes_cli.profiles import get_profile_dir
    from tui_gateway.kanban_notice_session_store import session_store

    with session_store(get_profile_dir(profile) / "state.db", sid) as pair:
        yield pair


def _ledger_settled(conn: Optional[sqlite3.Connection], sessions: tuple, ref: str, state: str) -> bool:
    """True when the ledger already settled this notice for one of ``sessions``: acked after a transcript write,
    or recorded silently by ``collect`` (stale, or covered by a local placeholder). A legacy pre-ack
    ``delivered=1`` row (no ``notice_key``) is not trusted; the transcript key decides for those."""
    if conn is None:
        return False
    marks = ",".join("?" * len(sessions))
    row = conn.execute(
        f"SELECT 1 FROM notices WHERE session IN ({marks}) AND ref=? AND state=? AND delivered IN (?, ?) "
        "AND notice_key IS NOT NULL LIMIT 1", (*sessions, ref, state, DELIVERED, SETTLED)).fetchone()
    return row is not None


def _current_states(rows: list[tuple]) -> dict:
    """``{ref: (state, task)}`` for every canonical replica card, terminal or not, keyed like the ledger."""
    out: dict[str, tuple] = {}
    for owner, key, _seq, raw in rows:
        try:
            task = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(task, dict) or not task.get("status"):
            continue
        out[f"{owner}/{key}"] = (_notice_state(task), task)
    return out


def _stale_notice_id(row, task: dict) -> tuple[str, str]:
    """``(profile, notice_id)`` the sweep would have used for this ledger row, for the ``swept`` record."""
    sid, key = str(row["session"]), str(row["notice_key"] or "")
    profile = next((str(link.get("profile") or "default") for link in task.get("sessions") or []
                    if isinstance(link, dict) and link.get("session") == sid), "")
    if not profile and key.endswith(f":{sid}"):
        profile = key[:-len(sid) - 1].rsplit(":", 1)[-1]
    profile = profile or "default"
    if str(row["state"]).startswith(("done@", "request_withdrawn@")) and key and not key.endswith(f":{profile}:{sid}"):
        return profile, f"{key}:{profile}:{sid}"
    return profile, key or f"remote:{row['ref']}:{row['state']}:{profile}:{sid}"


def _settle_stale_claims(states: dict, path, ledger) -> int:
    """Settle in-flight claims nobody can finish: the holder is dead AND the card left the notice's state. [daveey]

    ``collect`` and the sweep only walk cards that are terminal NOW, so a claim whose holder died after the card
    moved on (blocked, then unblocked or rescheduled) was never looked at again: it sat ``delivered=0`` with a dead
    holder forever and showed up in every audit (MacBook row 842, t_01a18622e706e70a, held >24 h). Such a row is
    settled as stale (``delivered=SETTLED``, holder cleared, ``settled='stale'``) with a ``swept`` row of reason
    ``stale`` in the same transaction. Its text is never delivered: it describes a state the card is no longer in,
    so it never counts as DELIVERED (that means "in the transcript"; row 842 was re-settled as delivered=1 with
    swept reason=delivered after 9eb41135c6). A live holder, a card still in that state (the re-offer path owns
    it) and a card missing from the replica (partial sync) are left alone. Never raises.
    """
    from tui_gateway.remote_sweep_projection import CompactTask

    try:
        conn = _ledger(ledger)
        try:
            claims = conn.execute(
                "SELECT session, ref, state, holder_pid, holder_start, notice_key FROM notices "
                "WHERE delivered=0 AND holder_pid IS NOT NULL").fetchall()
            settled = 0
            for row in claims:
                current = states.get(str(row["ref"]))
                if current is None or current[0] == row["state"]:
                    continue
                if _holder_is_me(row["holder_pid"]) or _holder_alive(row["holder_pid"], row["holder_start"]):
                    continue
                task = current[1]
                if isinstance(task, CompactTask):  # [daveey] a bystander row keeps no sessions: read them back
                    task = task.load(path)
                    if task is None:
                        continue  # the row moved since this snapshot: the next sweep settles it from its own
                profile, notice_id = _stale_notice_id(row, task)
                swept_key = (str(path), profile, str(row["session"]), notice_id)
                with conn:
                    cur = conn.execute(
                        "UPDATE notices SET delivered=?, holder_pid=NULL, holder_start=NULL, legacy_seen=1, "
                        "settled='stale', notice_key=IFNULL(notice_key, ?) WHERE session=? AND ref=? AND state=? "
                        "AND delivered=0 AND holder_pid IS ?",
                        (SETTLED, f"stale:{row['ref']}:{row['state']}", row["session"], row["ref"], row["state"],
                         row["holder_pid"]))
                    if cur.rowcount == 1:
                        _swept_row(conn, swept_key, "stale")
                if cur.rowcount != 1:
                    continue
                settled += 1
                logger.info("remote kanban: settled stale claim %s %s for chat %s (holder %s dead, card now %s)",
                            row["ref"], row["state"], row["session"], row["holder_pid"], current[0])
        finally:
            conn.close()
        return settled
    except Exception:
        logger.debug("remote kanban stale-claim settle failed", exc_info=True)
        return 0


def _sweep_payload(owner: str, key: str, task: dict, me: str, profile: str, sid: str) -> tuple:
    """``(event_key, metadata, text)`` of one swept notice, keyed like ``collect``'s."""
    withdrawal = _withdrawal(task)
    if withdrawal is not None:
        event_key = _event_key(owner, key, withdrawal)
        metadata = {"notice_id": _notice_id(owner, key, task, profile, sid),
                    "board": task.get("board", "default"), "task_id": task["id"],
                    "gateway": owner, "event_ids": [withdrawal["event_id"]],
                    "kanban_event_keys": [event_key], "blocked_on_human": False,
                    "event_kind": "request_withdrawn"}
        return event_key, metadata, format_notice(owner, key, task)
    notice = task.get("completion_notice")
    if task.get("status") == "done":
        event_id = notice["event_id"]
        event_key = _event_key(owner, key, notice)
        metadata = {"notice_id": _notice_id(owner, key, task, profile, sid),
                    "board": task.get("board", "default"), "task_id": task["id"],
                    "gateway": owner, "event_ids": [event_id], "kanban_event_keys": [event_key],
                    "blocked_on_human": False}
        return event_key, metadata, _completion_text(owner, key, task, me, profile, sid)
    event_key = _shared_key(owner, key, task, profile, sid)
    metadata = {"notice_id": event_key, "board": task.get("board", "default"),
                "task_id": task["id"], "gateway": owner, "kanban_event_keys": [event_key],
                "blocked_on_human": remote_blocked_on_human(task)}
    return event_key, metadata, format_notice(owner, key, task)


def _sweep_cards(rows: list[tuple], me: str):
    """Canonical terminal cards linked here, before freshness or open-chat filtering."""
    for owner, key, _seq, raw in rows:
        try:
            task = json.loads(raw)
        except (TypeError, ValueError):
            continue
        notice = task.get("completion_notice") if isinstance(task, dict) else None
        done = task.get("status") == "done" and isinstance(notice, dict) and "event_id" in notice
        if not done and task.get("status") != "blocked" and _withdrawal(task) is None:
            continue
        links = [{"profile": profile, "session": sid} for profile, sid in _linked_chats(task, me)]
        if not links:
            continue
        yield owner, key, task, links


def _settle_claim(claim: dict, memo_key: tuple, ledger) -> None:
    """The notice is in the chat's transcript (a written row, or a started turn's row found by its key): ledger
    delivered, memo and ``swept`` set."""
    ack(claim, ledger=ledger)
    keys = claim.get("memo_keys", [memo_key])
    _record_swept(keys, ledger, reason="delivered")
    for key in keys:
        _remember_delivered(key)


def _settle_started(session_db, profile: str, sid: str, event_key: str, claim: dict, memo_key: tuple,
                    ledger) -> bool:
    """A woken turn was ADMITTED: ack only if its notice row is already durable, else leave the claim in flight.

    [daveey] ``_RemoteWaker`` calls this when ``_wake_session_turn`` returns "started", which means the turn
    passed admission (``_notif_submit``); the turn's ``kanban_notice`` user row is written later, on the turn
    thread (``prompt_turn._invoke_agent``). Acking here wrote DELIVERED ("in the transcript") with no row, so a
    backend that died in between lost the notice for good (the failure class of ledger row 1245). A claim left
    in flight is acked by this process's next pass once the key lands (``_claim_notice``, holder-is-me branch:
    the sweep, or ``collect`` once the woken chat is live here); if this process dies first, the successor's
    dead-holder check finds no key in the transcript and re-offers it. Never raises.

    [daveey] The claim also follows the woken turn (``_track_woken``): the live session is looked up BEFORE the
    transcript check, so a turn that ends with no row (context refused, an error before the persist) is
    re-offered by this process's next pass instead of waiting for the backend to exit."""
    if claim.get("children"):
        results = [_settle_started(session_db, profile, child["session"], child["event_key"], child, key, ledger)
                   for child, key in zip(claim["children"], claim["memo_keys"])]
        return all(results)
    tip = None
    session = _live_woken_session(sid)
    try:
        with session_db(profile, sid) as (db, tip):
            durable = db is not None and notice_in_transcript(db, tip, event_key, "")
    except Exception:
        logger.debug("remote kanban: transcript check after a started wake failed", exc_info=True)
        durable = False
    if durable:
        _settle_claim(claim, memo_key, ledger)
        return True
    session = session or (_live_woken_session(tip) if tip and tip != sid else None)
    if session is not None:
        _track_woken((claim["session"], claim["ref"], claim["state"]), session)
    else:
        logger.info("remote kanban notice %s %s: woken chat %s is not live here; claim waits for its row or this "
                    "backend's exit", claim["ref"], claim["state"], sid)
    logger.info("remote kanban notice %s %s for chat %s: turn admitted, row not yet written; claim stays in "
                "flight until it is", claim["ref"], claim["state"], sid)
    return False


def _persist_claimed(session_db, profile: str, sid: str, text: str, metadata: dict, claim: dict, memo_key: tuple,
                     ledger) -> bool:
    """The passive row for a claimed notice whose wake did not start. A chat mid-turn (or any failure) hands the
    claim back, so the next pass (or the chat's live poller) delivers it instead of nobody. Never raises."""
    from hermes_state_errors import SessionTurnLeaseLostError
    try:
        with session_db(profile, sid) as (db, tip):
            from tui_gateway.notice_fanout import chat_skip_reason
            if db is None or chat_skip_reason(db, sid) is not None:
                release(claim, ledger=ledger)
                return False
            from tui_gateway.kanban_notice_session_store import notice_writer
            with notice_writer(db) as wdb:
                wdb.append_kanban_notice(tip, text, metadata)
                from hermes_state_outcome import refresh_outcomes_quietly
                refresh_outcomes_quietly([tip], "notice", db=wdb)  # [daveey] the passive row is the chat's last word
    except SessionTurnLeaseLostError:
        release(claim, ledger=ledger)
        logger.info("remote Kanban notice %s for chat %s deferred: a turn is running; retrying next pass",
                    claim["ref"], sid)
        return False
    except Exception:
        release(claim, ledger=ledger)
        logger.warning("remote Kanban notice %s for chat %s not written; retrying next pass", claim["ref"], sid,
                       exc_info=True)
        return False
    _settle_claim(claim, memo_key, ledger)
    return True


def _settle_unread(ledger, *, session: str, ref: str, state: str, now: float, notice_key: str, swept_key: tuple,
                   reason: str) -> bool:
    """Record a notice no chat will read (its target is a worker transcript) as SETTLED, with a ``swept`` row of
    ``reason``: no transcript row, no wake, and no later pass offers it again. An existing claim in flight or a
    DELIVERED row is left as it is. Returns whether this call settled it. Never raises. [daveey]

    Hub card t_a989e5e0 (2026-10-31): its done notice was written into five MacBook worker transcripts that held
    shepherd subs, and the wake of each was refused (``session.resume`` 4132)."""
    try:
        with contextlib.closing(_ledger(ledger)) as conn:
            with conn:
                before = conn.total_changes
                conn.execute(
                    "INSERT OR IGNORE INTO notices (session, ref, state, at, delivered, notice_key, legacy_seen, "
                    "settled) VALUES (?,?,?,?,?,?,1,?)", (session, ref, state, now, SETTLED, notice_key, reason))
                if conn.total_changes == before:
                    conn.execute(
                        "UPDATE notices SET delivered=?, settled=?, notice_key=IFNULL(notice_key, ?) "
                        "WHERE session=? AND ref=? AND state=? AND delivered=0 AND holder_pid IS NULL",
                        (SETTLED, reason, notice_key, session, ref, state))
                changed = conn.total_changes != before
                if changed:
                    _swept_row(conn, swept_key, reason)
        if changed:
            logger.info("remote kanban notice %s %s not delivered to %s: %s", ref, state, session, reason)
        else:
            _remember_delivered(tuple(swept_key))
        return changed
    except Exception:
        logger.debug("remote kanban: settling an unread notice failed", exc_info=True)
        return False


def _deliver_link(owner, key, task, link, *, me, ref, state, path, live, session_db, ledger, settled,
                  delivered: list, swept: list, waker=None, observed_at=None, now=None) -> None:
    """Deliver one fresh notice unless the memo or the ledger already settled it. A failed write is not remembered.

    Without ``waker`` the notice is a passive row. With one (the production sweeper, ``kanban_unloaded``) the
    notice is claimed in the ledger, like ``collect`` claims it, and handed to the waker, which starts ONE turn
    with it and falls back to the row. A row alone is not a turn: an idle chat sat on two hub cards' rows for
    2-3.5 h until an unrelated local card woke it (David-MBP 2026-10-29). A state younger than the waker's grace
    window is left for the chat's live poller, which may run in another backend on this host. [daveey]"""
    profile, sid = link.get("profile"), link.get("session")
    from hermes_cli.kanban_placement import is_placeholder_session
    if is_placeholder_session(sid):
        logger.warning("Dropped Kanban notice to placement placeholder %s", sid)
        return
    if not profile or not sid:
        return
    try:
        memo_key = (str(path), profile, sid, _notice_id(owner, key, task, profile, sid))
        if memo_key in _delivered:
            return
        if sid in live:
            _note_open_chat(sid, ref, state, waker, observed_at, now, ledger)
            return
        if waker is not None and observed_at is not None and now is not None and now - observed_at < waker.grace_s:
            return  # a live poller (any backend) claims within seconds; it owns the chat's turn
        event_key, metadata, text = _sweep_payload(owner, key, task, me, profile, sid)
        claim = {"session": sid, "ref": ref, "state": state, "notice_key": event_key}
        with session_db(profile, sid) as (db, tip):
            if db is None or not text:
                return
            from tui_gateway.notice_fanout import chat_skip_reason, worker_transcript_reason
            if chat_skip_reason(db, sid) is not None:  # [daveey] archived chat or a copy moved to another gateway
                return
            if (why := worker_transcript_reason(db, sid)) is not None:
                _settle_unread(ledger, session=sid, ref=ref, state=state, now=now or time.time(),
                               notice_key=event_key, swept_key=memo_key, reason=why)
                return
            if settled((sid, tip), ref, state):
                _remember_delivered(memo_key)
                return
            from tui_gateway.kanban_remote_fifo import gateway_holds, gateway_lease_pid, lineage_ids
            ids = lineage_ids(db, sid, tip)
            if gateway_holds(db, ids, event_key):
                # [daveey] The gateway runs this chat's turns and admits the notice to its FIFO
                # (gateway.kanban_remote_owner); a claim here would only race it (t_01a1cff0541bf30c).
                _note_wait(sid, ref, state, "left to the gateway's FIFO (the gateway runs this chat's turns)")
                return
            if (gw_pid := gateway_lease_pid(db, ids, profile)) is not None:
                # [daveey] A gateway turn holds the chat: no row or wake can land here until it ends, and the
                # gateway's owner pass admits the notice meanwhile. No claim, so nothing races it (G3).
                _note_wait(sid, ref, state, f"left to gateway pid {gw_pid}: its turn holds the chat's lease")
                return
            if waker is None:
                from tui_gateway.kanban_notice_session_store import notice_writer
                with notice_writer(db) as wdb:
                    row_id = wdb.append_kanban_notice(tip, text, metadata)
                    from hermes_state_outcome import refresh_outcomes_quietly
                    refresh_outcomes_quietly([tip], "notice", db=wdb)  # [daveey] unloaded sweeper's remote rows too
                delivered.append({"task_id": task["id"], "session_id": tip, "message_row": row_id})
                swept.append(memo_key)
                ack(claim, ledger=ledger)
                _remember_delivered(memo_key)
                return
            with contextlib.closing(_ledger(ledger)) as conn:
                verdict = _claim_notice(conn, session=sid, ref=ref, state=state, now=now or time.time(), fresh=True,
                                        notice_key=event_key, task_id=str(task["id"]),
                                        transcript_has=lambda k, t: notice_in_transcript(db, tip, k, t),
                                        swept_key=memo_key)
                waiting = _wait_reason(conn, sid, ref, state) if verdict == "skip" else None
        if verdict == "acked":
            return  # _claim_notice recorded it in ``swept`` (reason transcript) and the memo
        if verdict != "offer":
            if waiting:  # another consumer holds it in flight: name it, once
                _note_wait(sid, ref, state, waiting)
            return
        extra = {"defer": lambda: release(claim, ledger=ledger)} if getattr(waker, "supports_defer", False) else {}
        result = waker.deliver(
            tip, text, metadata, task, now or time.time(),
            persist=lambda: _persist_claimed(session_db, profile, sid, text, metadata, claim, memo_key, ledger),
            started=lambda: _settle_started(session_db, profile, sid, event_key, claim, memo_key, ledger),
            **extra)
        delivered.append({"task_id": task["id"], "session_id": tip, "wake": result})
    except Exception:
        logger.debug("remote Kanban notice deferred for %s/%s", owner, key, exc_info=True)


def _deliver_fresh(cards, fresh_times, me, now, path, live, session_db, ledger, settled, delivered, swept,
                   counts, waker=None) -> None:
    from tui_gateway.kanban_remote_late import SweepBatches

    batches = SweepBatches(waker, session_db, ledger, now)
    try:
        _deliver_fresh_cards(cards, fresh_times, me, now, path, live, session_db, ledger, settled, delivered,
                             swept, counts, waker, batches)
    finally:  # claims taken before an error still reach the flush, which delivers or releases them
        delivered.extend(batches.flush())


def _deliver_fresh_cards(cards, fresh_times, me, now, path, live, session_db, ledger, settled, delivered, swept,
                         counts, waker, batches) -> None:
    from tui_gateway.kanban_remote_late import owner_time

    for owner, key, task, links in cards:
        ref, state = f"{owner}/{key}", _notice_state(task)
        observed_at = fresh_times.get((owner, key, state), _fresh_at(task))
        first_seen = _observed_first_seen(fresh_times, (owner, key, state))
        overdue = observed_at is None or now - observed_at > FRESH_S
        if overdue and (first_seen is None or _late_verdict(now, first_seen) != "late"):
            counts["stale"] = counts.get("stale", 0) + 1
            continue
        for link in links:
            if overdue and _late_link_settled(owner, key, task, link, ref, state, path, settled):
                continue  # [daveey] one ledger read, then the memo: a settled late card never reopens state.db
            late = owner_time(task, now) is not None
            target = batches.target(owner, key, task, link, path) if late else waker
            results = [] if late else delivered
            _deliver_link(owner, key, task, link, me=me, ref=ref, state=state, path=path, live=live,
                          session_db=session_db, ledger=ledger, settled=settled,
                          delivered=results, swept=swept, waker=target, observed_at=observed_at, now=now)


def _late_link_settled(owner, key, task, link, ref, state, path, settled) -> bool:
    """An observed card past ``FRESH_S`` that this link already settled (delivered, stale or by a placeholder)
    under its own session id. Checked before ``_deliver_link`` opens the chat's state.db, and remembered."""
    profile, sid = link.get("profile"), link.get("session")
    if not profile or not sid:
        return False
    memo_key = (str(path), profile, sid, _notice_id(owner, key, task, profile, sid))
    if memo_key in _delivered:
        return True
    if settled((sid,), ref, state):
        _remember_delivered(memo_key)
        return True
    return False


def _sweep_snapshot(path, me: str) -> tuple:
    """Cache only replica-derived data, never claim recovery or time/liveness decisions.

    Bracket the snapshot with SQLite's published-commit key, not WAL timestamps.
    One memo bounds retained data; a failed/racing read cannot bless a stale snapshot.
    """
    global _sweep_memo
    from tui_gateway.kanban_replica_observer import file_identity, fingerprint
    from tui_gateway import remote_sweep_projection

    stamp = fingerprint(path)
    memo = _sweep_memo
    if stamp is not None and memo is not None and memo[:2] == (stamp, me):
        return memo[2], memo[3]
    _sweep_memo = None
    with contextlib.closing(_ro(Path(path))) as conn:
        conn.execute("BEGIN")
        cards, states = remote_sweep_projection.snapshot(
            conn, file_identity(stamp), me)
    if stamp is not None and fingerprint(path) == stamp:
        _sweep_memo = (stamp, me, cards, states)
    return cards, states


def sweep_remote_notices(*, replica_path=None, gateway=None, session_db=_session_db, live_ids=None,
                         ledger=None, now: Optional[float] = None, waker=None) -> list[dict]:
    """Persist addressed notices, then ack the ledger. Write-first: a failed append leaves no delivered row
    and the next sweep retries (no claim-before-durable). ``live_ids`` are open chats; their poller writes the
    row and starts the turn, so this sweep skips them. Done and blocked share one ``kanban_event_keys`` value
    with ``collect``, so running both yields one transcript row.

    [daveey] Same guards as ``collect``: one canonical replica row per card (a frozen legacy-key row is never
    read), only states fresh by owner time or durable local arrival, and nothing the ledger already settled.
    A successful append is also recorded in the ledger's ``swept`` table, so a restart does not reopen
    state.db for a notice this process already persisted. Without the guards a rollout that changed the
    blocked notice key replayed 293 ``blocked`` notices from days earlier, of cards long done, into idle
    chats (MacBook 2026-10-29 00:22Z).

    ``waker`` (``kanban_unloaded._RemoteWaker``, passed by the production sweeper) turns each fresh notice into ONE
    turn in the chat, after a grace window for its live poller; without it the notice is only the passive row.
    """
    from hermes_cli.kanban_origin import _hub_db, gateway_names

    path = replica_path if replica_path is not None else _hub_db()
    if path is None:
        return []
    me = gateway if gateway is not None else gateway_names()[0]
    if not me:
        return []
    now = time.time() if now is None else now
    live = set(live_ids or ())
    try:
        cards, states = _sweep_snapshot(path, me)
    except sqlite3.Error:
        logger.debug("remote Kanban replica unavailable", exc_info=True)
        return []
    from tui_gateway.kanban_remote_observations import observe

    try:
        fresh_times = observe(Path(path), ledger, now)
    except (OSError, sqlite3.Error):
        logger.debug("remote kanban observation failed; defer notices", exc_info=True)
        return []
    _load_swept(ledger)
    _settle_stale_claims(states, path, ledger)
    delivered: list[dict] = []
    swept: list = []
    counts: dict = {}
    try:
        with contextlib.ExitStack() as stack:
            ledger_conn: list = []

            def settled(sessions: tuple, ref: str, state: str) -> bool:
                if not ledger_conn:
                    try:
                        ledger_conn.append(stack.enter_context(contextlib.closing(_ledger(ledger))))
                    except (OSError, sqlite3.Error):
                        logger.debug("remote kanban ledger unreadable; transcript keys dedupe alone", exc_info=True)
                        ledger_conn.append(None)
                return _ledger_settled(ledger_conn[0], sessions, ref, state)

            _deliver_fresh(cards, fresh_times, me, now, path, live, session_db, ledger, settled, delivered, swept, counts,
                           waker=waker)
        stale = counts.get("stale", 0)
        if stale and stale != _last_stale.get(str(path)):
            _last_stale[str(path)] = stale
            logger.info("remote kanban sweep: %d linked terminal card(s) older than %.0fh not announced",
                        stale, FRESH_S / 3600)
    finally:
        _record_swept(swept, ledger, reason="delivered")
    return delivered
