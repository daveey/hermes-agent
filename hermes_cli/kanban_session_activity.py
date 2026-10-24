"""Which sessions still have kanban work in flight.

A session that hands work to ``kanban_create`` ends its own turn right away,
while the tasks it created keep running for hours in worker processes. The
sidebar painted such a session as "done" (green stripe) although its work was
still going on. Every kanban task records the session that created it
(``tasks.session_id``), so session-list endpoints stamp each row with how many
of its tasks are still pending or running (``kanban_active``); the desktop
treats a non-zero count like a running background job. [daveey]

Read-only and cheap: one indexed GROUP BY per board database, cached for a few
seconds because the sidebar polls.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

_log = logging.getLogger(__name__)

# Base queued/running statuses. _status_filter also counts nonhuman blocked
# and triage work as active; only explicit human-kind triage paints red.
ACTIVE_STATUSES = ("todo", "ready", "running", "scheduled")
# Waiting on a human. These don't keep the session busy, but the sidebar row
# shows how many there are next to the open count (``kanban_blocked``) and the
# row paints red. [daveey]
# ``triage`` joins it only for a card block-loop detection escalated there: it
# keeps its human ``block_kind`` and still waits on the user. A card created
# in triage for a specifier (``block_kind`` NULL) is not red. [daveey]
BLOCKED_STATUSES = ("blocked", "triage")
# A ``blocked`` card only waits on a human for these kinds (NULL = generic human
# blocker). A ``transient``/``time`` block waits on a retry or a clock, so it
# counts as in-progress work instead, like ``scheduled``. [daveey]
HUMAN_BLOCK_KINDS = ("needs_input", "capability")

_CACHE_TTL_S = 3.0
_cache_lock = threading.Lock()
_cache: tuple[float, dict[str, int], dict[str, int], dict[str, int]] | None = None
_waiting_cache: list[dict[str, frozenset[str]]] = [{}]
_pairs_lock = threading.Lock()
WAITING_ON_CAP = 50


def _board_db_paths() -> list[Path]:
    from hermes_cli import kanban_db

    paths: list[Path] = []
    root = kanban_db.kanban_home()
    default = root / "kanban.db"
    if default.exists():
        paths.append(default)
    try:
        boards = kanban_db.boards_root()
        if boards.is_dir():
            paths.extend(p for p in sorted(boards.glob("*/kanban.db")) if p.is_file())
    except OSError:
        pass
    return paths


def _status_filter(conn: sqlite3.Connection, statuses: tuple[str, ...]) -> tuple[str, tuple]:
    """Split open work by human responsibility, including escalated triage.

    Legacy boards have no explicit human triage kinds, so triage stays active.
    """
    ph = ",".join("?" for _ in statuses)
    if not {r[1] for r in conn.execute("PRAGMA table_info(tasks)")} >= {"block_kind"}:
        if statuses == BLOCKED_STATUSES:
            return "t.status = 'blocked'", ()
        return f"(t.status IN ({ph}) OR t.status = 'triage')", statuses
    kinds = ",".join("?" for _ in HUMAN_BLOCK_KINDS)
    human = f"(t.block_kind IS NULL OR t.block_kind IN ({kinds}))"
    if statuses == BLOCKED_STATUSES:
        escalated = f"(t.status = 'triage' AND t.block_kind IN ({kinds}))"
        return f"((t.status = 'blocked' AND {human}) OR {escalated})", (*HUMAN_BLOCK_KINDS, *HUMAN_BLOCK_KINDS)
    # Triage is unfinished even without a worker. Only explicit human kinds
    # belong to the blocked bucket; NULL triage awaits an agent specifier.
    triage = f"(t.status = 'triage' AND (t.block_kind IS NULL OR t.block_kind NOT IN ({kinds})))"
    return (f"(t.status IN ({ph}) OR (t.status = 'blocked' AND NOT {human}) OR {triage})",
            (*statuses, *HUMAN_BLOCK_KINDS, *HUMAN_BLOCK_KINDS))


# Set while ``_all_counts`` reads local boards: collects the (session, task) pairs behind the counts, so the
# open card ids per session (``waiting_on_task_ids``) cost no extra queries. [daveey]
_pairs_sink: set[tuple[str, str]] | None = None


# Subscription reasons that make a ``tui`` subscriber own the card's progress (stamped in
# ``delivery_metadata.reason``): ``create`` (kanban_create's auto-sub) and ``shepherd``
# (explicit ``notify-subscribe``). ``touch`` (auto-sub on comment/unblock/link) and legacy rows
# with no reason still get completion pings, but never keep a session busy or paint it red unless
# the chat owns the card (``tasks.session_id``). [daveey]
COUNTED_SUB_REASONS = ("create", "shepherd")


def _counted_sub_clause(conn: sqlite3.Connection) -> str:
    """SQL predicate (aliases ``s`` subs, ``t`` tasks) for a ``tui`` sub that counts as the chat's work."""
    owner = "s.chat_id = COALESCE(t.session_id, '')"
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(kanban_notify_subs)")}
    except sqlite3.Error:
        cols = set()
    if "delivery_metadata" not in cols:
        return f"({owner})"
    reasons = ", ".join(f"'{r}'" for r in COUNTED_SUB_REASONS)
    return (f"({owner} OR (json_valid(s.delivery_metadata) "
            f"AND json_extract(s.delivery_metadata, '$.reason') IN ({reasons})))")


def _read_counts(path: Path, statuses: tuple[str, ...] = ACTIVE_STATUSES) -> dict[str, int]:
    """Tasks per session in ``statuses``: tasks the session created, plus tasks
    it shepherds (a ``tui`` notify sub stamped ``create``/``shepherd``, see
    ``COUNTED_SUB_REASONS``): it is just as "still working" until they land.
    A touch-only sub (one comment on someone else's card) does not count.
    Each (session, task) pair counts once."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    try:
        match, params = _status_filter(conn, statuses)
        pairs: set[tuple[str, str]] = {
            (str(sid), str(tid)) for sid, tid in conn.execute(
                f"SELECT session_id, id FROM tasks t WHERE session_id IS NOT NULL "
                f"AND session_id != '' AND {match}",
                params,
            )
        }
        try:
            pairs.update(
                (str(sid), str(tid)) for sid, tid in conn.execute(
                    f"SELECT s.chat_id, t.id FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
                    f"WHERE s.platform = 'tui' AND s.chat_id != '' AND {_counted_sub_clause(conn)} AND {match}",
                    params,
                )
            )
        except sqlite3.Error:  # board predates notify subscriptions
            pass
    finally:
        conn.close()
    counts: dict[str, int] = {}
    for sid, _tid in pairs:
        counts[sid] = counts.get(sid, 0) + 1
    if _pairs_sink is not None:
        _pairs_sink.update(pairs)
    return counts


# Terminal kinds that produce a visible notice (mirrors tui_gateway.session_notifications; silent kinds such as
# archived/unblocked never leave a notice waiting).
UNSEEN_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out", "status")


def _read_unseen(path: Path) -> dict[str, int]:
    """``tui`` subscriptions per chat whose cursor trails a terminal event: a result the session has not been
    handed yet (its backend is gone, so no poller claims it until the unloaded-session sweeper does). [daveey]"""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    try:
        ph = ",".join("?" for _ in UNSEEN_KINDS)
        rows = conn.execute(
            f"SELECT s.chat_id, COUNT(*) FROM kanban_notify_subs s WHERE s.platform = 'tui' AND s.chat_id != '' "
            f"AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = s.task_id AND e.id > s.last_event_id "
            f"AND e.kind IN ({ph})) GROUP BY s.chat_id", UNSEEN_KINDS).fetchall()
    except sqlite3.Error:  # board predates notify subscriptions
        return {}
    finally:
        conn.close()
    return {str(sid): int(n) for sid, n in rows}


# [daveey] Cards on ANOTHER gateway. A session here can own work that runs elsewhere (``hermes-hub create-task``,
# a ``shepherd`` request, ``cross_task`` read/comment): the owner publishes it as a ``sessions`` link
# ``{session, gw: <this gateway>, rel: "shepherd"}`` on the card, and hermes-hub keeps a local replica of every
# gateway's cards in ``hub.db``. Those cards (and their children, transitively) count for the session too, so it
# doesn't paint green while its remote work runs. Read-only; no replica = no-op.
def _hub_db_path() -> Path | None:
    base = os.environ.get("HERMES_HUB_STATE") or str(Path.home() / ".local" / "state" / "hermes-hub")
    path = Path(base) / "hub.db"
    return path if path.is_file() else None


def _open_ro(path: Path) -> sqlite3.Connection:
    """Read-only open that survives a WAL db whose -wal/-shm were just removed (plain ``mode=ro`` then fails);
    never creates the file."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn
    except sqlite3.OperationalError:
        conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=0.5)
        conn.execute("PRAGMA query_only = ON")
        return conn


def _self_gateway(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("SELECT v FROM meta WHERE k = 'gateway'").fetchone()
        if row and row[0]:
            return str(row[0])
    except sqlite3.Error:
        pass
    try:
        cfg = Path(os.environ.get("HERMES_MAIL_CONFIG") or Path.home() / ".config" / "hermes-mail" / "config.json")
        return str(json.loads(cfg.read_text(encoding="utf-8-sig")).get("gateway") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _replica_bucket(task: dict[str, Any]) -> str | None:
    """'active' | 'blocked' | None, by the same rules as ``_status_filter`` for local boards."""
    status, kind = task.get("status"), task.get("block_kind")
    human = kind is None or kind in HUMAN_BLOCK_KINDS
    if (status in ACTIVE_STATUSES or (status == "blocked" and not human)
            or (status == "triage" and kind not in HUMAN_BLOCK_KINDS)):
        return "active"
    if (status == "blocked" and human) or (status == "triage" and kind in HUMAN_BLOCK_KINDS):
        return "blocked"
    return None


def _read_hub_replica(path: Path) -> tuple[dict[str, int], dict[str, int]]:
    """``(active, blocked)`` per local session from replica cards whose ``sessions`` link names this gateway,
    plus their children (walked transitively on the same gateway and board)."""
    conn = _open_ro(path)
    try:
        me = _self_gateway(conn)
        if not me:
            return {}, {}
        rows = conn.execute("SELECT gw, data FROM replica WHERE coll = 'tasks' AND gw != ?", (me,)).fetchall()
    finally:
        conn.close()
    tasks: dict[tuple[str, str, str], dict[str, Any]] = {}
    for gw, data in rows:
        try:
            t = json.loads(data)
        except (TypeError, ValueError):
            continue
        if isinstance(t, dict) and t.get("id"):
            tasks[(str(gw), str(t.get("board") or "default"), str(t["id"]))] = t
    pairs: dict[str, set[tuple[str, str]]] = {"active": set(), "blocked": set()}
    for (gw, board, tid), t in tasks.items():
        links = t.get("sessions") if isinstance(t.get("sessions"), list) else []
        sids = {str(l["session"]) for l in links if isinstance(l, dict) and l.get("session") and l.get("gw") == me}
        if not sids:
            continue
        seen: set[str] = set()
        todo = [tid]
        while todo:
            cur = todo.pop()
            if cur in seen:
                continue
            seen.add(cur)
            card = tasks.get((gw, board, cur))
            if card is None:
                continue
            bucket = _replica_bucket(card)
            if bucket:
                ref = f"{gw}/{board}/{cur}"
                pairs[bucket].update((sid, ref) for sid in sids)
            kids = card.get("children") if isinstance(card.get("children"), list) else []
            todo.extend(str(k) for k in kids if k)
    out: list[dict[str, int]] = []
    for bucket in ("active", "blocked"):
        counts: dict[str, int] = {}
        for sid, _ref in pairs[bucket]:
            counts[sid] = counts.get(sid, 0) + 1
        out.append(counts)
    return out[0], out[1]


def _all_counts() -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """``(active, blocked, unseen)`` per-session task counts across every board, cached."""
    global _cache
    now = time.monotonic()
    with _cache_lock:
        if _cache is not None and now < _cache[0]:
            return _cache[1], _cache[2], _cache[3]
    active: dict[str, int] = {}
    blocked: dict[str, int] = {}
    unseen: dict[str, int] = {}
    pairs = _read_local_pairs_into(active, blocked, unseen)
    waiting: dict[str, set[str]] = {}
    for sid, tid in pairs:
        waiting.setdefault(sid, set()).add(tid)
    try:
        hub = _hub_db_path()
        if hub is not None:
            r_active, r_blocked = _read_hub_replica(hub)
            for src, into in ((r_active, active), (r_blocked, blocked)):
                for sid, n in src.items():
                    into[sid] = into.get(sid, 0) + n
    except Exception as exc:  # a corrupt/locked replica must never break the sidebar
        _log.debug("hermes-hub replica read failed: %s", exc)
    with _cache_lock:
        _cache = (now + _CACHE_TTL_S, active, blocked, unseen)
        _waiting_cache[0] = {sid: frozenset(ids) for sid, ids in waiting.items()}
    return active, blocked, unseen


def _read_local_pairs_into(active: dict[str, int], blocked: dict[str, int],
                           unseen: dict[str, int]) -> set[tuple[str, str]]:
    """Fill the three count maps from every local board; return the open (session, task) pairs."""
    global _pairs_sink
    with _pairs_lock:
        _pairs_sink = set()
        try:
            _read_local_boards(active, blocked, unseen)
            return _pairs_sink
        finally:
            _pairs_sink = None


def _read_local_boards(active: dict[str, int], blocked: dict[str, int], unseen: dict[str, int]) -> None:
    for path in _board_db_paths():
        try:
            for sid, n in _read_unseen(path).items():
                unseen[sid] = unseen.get(sid, 0) + n
        except sqlite3.Error as exc:
            _log.debug("kanban unseen read failed for %s: %s", path, exc)
        for statuses, into in ((ACTIVE_STATUSES, active), (BLOCKED_STATUSES, blocked)):
            try:
                for sid, n in _read_counts(path, statuses).items():
                    into[sid] = into.get(sid, 0) + n
            except sqlite3.Error as exc:  # an old schema without session_id, a locked file
                _log.debug("kanban activity read failed for %s: %s", path, exc)


# [daveey] Extra "this chat still waits" sources (plugins): name -> fn() -> {session_id: iterable of task ids}. A
# chat-closeout plugin registers the cards of a settled batch whose close-out turn has not run yet, so the row
# stays grey "waiting" between the last notice and the closing answer instead of flashing green.
_waiting_sources: dict[str, Any] = {}


def register_waiting_source(name: str, fn) -> None:
    """Register (or replace, ``fn=None`` removes) a plugin source of waiting card ids per session. [daveey]"""
    if fn is None:
        _waiting_sources.pop(name, None)
    else:
        _waiting_sources[name] = fn


def _extra_waiting() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for name, fn in list(_waiting_sources.items()):
        try:
            for sid, ids in (fn() or {}).items():
                out.setdefault(str(sid), set()).update(str(i) for i in ids if i)
        except Exception:  # a plugin must never break the sidebar
            _log.debug("waiting source %s failed", name, exc_info=True)
    return out


def waiting_on_task_ids() -> dict[str, frozenset[str]]:
    """``{session_id: open task ids}`` on this gateway's boards: queued/running cards plus those waiting on a human,
    created or shepherded by the session — what the chat is waiting on. Same cache as the counts. [daveey]"""
    _all_counts()
    with _cache_lock:
        return dict(_waiting_cache[0])


def active_task_counts() -> dict[str, int]:
    """``{session_id: active task count}`` across every kanban board."""
    return _all_counts()[0]


def blocked_task_counts() -> dict[str, int]:
    """``{session_id: blocked task count}`` across every kanban board."""
    return _all_counts()[1]


# What a session's task strip lists: the sidebar's open + blocked sets, plus
# ``review`` (also waiting on a human). [daveey]
LISTED_STATUSES = ACTIVE_STATUSES + BLOCKED_STATUSES + ("review",)
_TASK_FIELDS = ("id", "title", "status", "assignee", "session_id", "created_at", "started_at")


def _board_slug(path: Path) -> str:
    return path.parent.name if path.parent.parent.name == "boards" else "default"


def _read_session_tasks(path: Path, ids: list[str], statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    """Tasks on one board that any of ``ids`` created or is a ``tui`` subscriber
    of, in ``statuses`` — the same pairs ``_read_counts`` counts."""
    sid_ph = ",".join("?" for _ in ids)
    st_ph = ",".join("?" for _ in statuses)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        select = ", ".join(f"t.{c}" if c in cols else f"NULL AS {c}" for c in _TASK_FIELDS)
        run_start = "NULL"
        if "current_run_id" in cols and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_runs'").fetchone():
            run_start = "(SELECT r.started_at FROM task_runs r WHERE r.id = t.current_run_id)"
        found: dict[str, dict[str, Any]] = {}
        for row in conn.execute(
                f"SELECT {select}, {run_start} AS current_run_started_at FROM tasks t "
                f"WHERE t.session_id IN ({sid_ph}) AND t.status IN ({st_ph})", (*ids, *statuses)):
            found[row["id"]] = {**dict(row), "via": "created"}
        try:
            for row in conn.execute(
                    f"SELECT {select}, {run_start} AS current_run_started_at FROM kanban_notify_subs s "
                    f"JOIN tasks t ON t.id = s.task_id WHERE s.platform = 'tui' AND {_counted_sub_clause(conn)} "
                    f"AND s.chat_id IN ({sid_ph}) AND t.status IN ({st_ph})", (*ids, *statuses)):
                found.setdefault(row["id"], {**dict(row), "via": "subscribed"})
        except sqlite3.Error:  # board predates notify subscriptions
            pass
    finally:
        conn.close()
    board = _board_slug(path)
    return [{**t, "board": board} for t in found.values()]


def session_tasks(session_ids: Iterable[str], statuses: tuple[str, ...] = LISTED_STATUSES) -> list[dict[str, Any]]:
    """Every open/blocked/review task across all boards that the given session
    ids (one compression lineage) created or shepherd — exactly the cards the
    sidebar row's ``kanban_active``/``kanban_blocked`` counts cover. Never
    raises on a bad board; skips it."""
    ids = [s for s in dict.fromkeys(str(s) for s in session_ids if s)]
    if not ids:
        return []
    out: list[dict[str, Any]] = []
    for path in _board_db_paths():
        try:
            out.extend(_read_session_tasks(path, ids, statuses))
        except sqlite3.Error as exc:
            _log.debug("kanban session tasks read failed for %s: %s", path, exc)
    return out


# [daveey] A "done" card can still need the user: its handoff admits part of the
# work was dropped, deferred or never run, and no follow-up card carries it.
# Core has no opinion on what counts; plugins register a check
# (``register_done_check``) that gets each recently completed card and returns
# True when its done is suspect. Rows of sessions with such a card get
# ``kanban_attention`` (count) and paint amber instead of green. Nothing
# registered = no field, no extra reads (upstream behaviour).
ATTENTION_WINDOW_S = 24 * 3600
_ATTENTION_TTL_S = 30.0
_done_checks: dict[str, Callable[[dict[str, Any]], bool]] = {}
_attention_cache: tuple[float, dict[str, int]] | None = None


def register_done_check(name: str, fn: Callable[[dict[str, Any]], bool]) -> Callable[[], None]:
    """Register ``fn(card) -> bool`` (True = this completed card needs attention). ``card`` has ``id``, ``title``,
    ``summary`` (latest run summary, else ``result``), ``completed_at``, ``board`` and ``board_path``. Re-registering a
    name replaces it; the returned callable unregisters."""
    global _attention_cache
    _done_checks[name] = fn
    _attention_cache = None

    def _dispose() -> None:
        global _attention_cache
        if _done_checks.get(name) is fn:
            _done_checks.pop(name, None)
            _attention_cache = None

    return _dispose


def _read_recent_done(path: Path, since: float) -> list[tuple[str, dict[str, Any]]]:
    """``(session_id, card)`` for done cards completed since ``since`` that a session created or shepherds."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    conn.row_factory = sqlite3.Row
    try:
        has_runs = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_runs'").fetchone() is not None
        summary = ("COALESCE((SELECT r.summary FROM task_runs r WHERE r.task_id = t.id AND r.summary IS NOT NULL "
                   "AND r.summary != '' ORDER BY r.id DESC LIMIT 1), t.result)") if has_runs else "t.result"
        fields = f"t.id AS id, t.title AS title, t.completed_at AS completed_at, {summary} AS summary"
        cond = "t.status = 'done' AND t.completed_at >= ?"
        pairs = [(str(r["session_id"]), dict(r)) for r in conn.execute(
            f"SELECT t.session_id AS session_id, {fields} FROM tasks t WHERE t.session_id IS NOT NULL "
            f"AND t.session_id != '' AND {cond}", (since,))]
        try:
            pairs.extend((str(r["chat_id"]), dict(r)) for r in conn.execute(
                f"SELECT s.chat_id AS chat_id, {fields} FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
                f"WHERE s.platform = 'tui' AND s.chat_id != '' AND {_counted_sub_clause(conn)} AND {cond}",
                (since,)))
        except sqlite3.Error:  # board predates notify subscriptions
            pass
    finally:
        conn.close()
    board = _board_slug(path)
    out = []
    for sid, card in pairs:
        card.pop("session_id", None)
        card.pop("chat_id", None)
        out.append((sid, {**card, "board": board, "board_path": str(path)}))
    return out


def attention_task_counts(now: float | None = None) -> dict[str, int]:
    """``{session_id: completed cards a registered check flags}`` over the last ``ATTENTION_WINDOW_S``. Cached;
    a check that raises counts as False."""
    global _attention_cache
    if not _done_checks:
        return {}
    mono = time.monotonic()
    with _cache_lock:
        if _attention_cache is not None and mono < _attention_cache[0]:
            return _attention_cache[1]
    since = (time.time() if now is None else now) - ATTENTION_WINDOW_S
    checks = list(_done_checks.values())
    verdicts: dict[tuple[str, str], bool] = {}
    pairs: set[tuple[str, str]] = set()
    for path in _board_db_paths():
        try:
            recent = _read_recent_done(path, since)
        except sqlite3.Error as exc:
            _log.debug("kanban attention read failed for %s: %s", path, exc)
            continue
        for sid, card in recent:
            key = (str(path), str(card["id"]))
            if key not in verdicts:
                flagged = False
                for check in checks:
                    try:
                        if check(card):
                            flagged = True
                            break
                    except Exception:
                        _log.debug("kanban done check failed on %s", card.get("id"), exc_info=True)
                verdicts[key] = flagged
            if verdicts[key]:
                pairs.add((sid, key[1]))
    counts: dict[str, int] = {}
    for sid, _tid in pairs:
        counts[sid] = counts.get(sid, 0) + 1
    with _cache_lock:
        _attention_cache = (mono + _ATTENTION_TTL_S, counts)
    return counts


def _row_ids(row: dict[str, Any]) -> Iterable[str]:
    seen: set[str] = set()
    for sid in (row.get("id"), row.get("_lineage_root_id"), *(row.get("_lineage_ids") or ())):
        if sid and sid not in seen:
            seen.add(sid)
            yield sid


def annotate_kanban_activity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp ``kanban_active``, ``kanban_blocked``, ``kanban_waiting_on`` (local open card ids), ``kanban_unseen`` (finished cards whose result the
    session has not been handed yet) and ``kanban_attention`` (done cards a registered check flags) on each session row
    (summed over its whole compression lineage). Never raises: the list must
    render without them."""
    if not rows:
        return rows
    try:
        active, blocked, unseen = _all_counts()
        waiting = waiting_on_task_ids()
        extra = _extra_waiting()
    except Exception:
        _log.debug("kanban activity unavailable", exc_info=True)
        return rows
    try:
        attention = attention_task_counts()
    except Exception:
        _log.debug("kanban attention unavailable", exc_info=True)
        attention = {}
    for row in rows:
        ids = list(_row_ids(row))
        for key, counts in (("kanban_active", active), ("kanban_blocked", blocked), ("kanban_unseen", unseen),
                            ("kanban_attention", attention)):
            n = sum(counts.get(sid, 0) for sid in ids)
            if n:
                row[key] = n
            else:
                row.pop(key, None)
        # [daveey] The open card ids the chat waits on (lineage-wide, capped): the sidebar's "waiting on N cards".
        open_ids = {tid for sid in ids for tid in waiting.get(sid, ())}
        # Plugin sources (pending close-outs): count the ids the boards don't already count as open.
        pending = {tid for sid in ids for tid in extra.get(sid, ())} - open_ids
        if pending:
            row["kanban_active"] = int(row.get("kanban_active") or 0) + len(pending)
        ids_waiting = sorted(open_ids | pending)
        if ids_waiting:
            row["kanban_waiting_on"] = ids_waiting[:WAITING_ON_CAP]
        else:
            row.pop("kanban_waiting_on", None)
    return rows


def _reset_cache_for_tests() -> None:
    global _cache, _attention_cache
    with _cache_lock:
        _cache = None
        _attention_cache = None
        _waiting_cache[0] = {}
