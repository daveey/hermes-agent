"""Which chats a card's change reaches, and what a change does to the chat's earlier notices. [daveey]

Session-outcome contract, "Fan-out" and "Stale notices":

- A notice or a card-change refresh never goes to a chat whose link to the card is released, nor to a chat row
  that is archived or was handed to another gateway (``end_reason`` ``moved_to_gateway:<gw>``, the copy left
  behind by ``session_handover``). One block once reached five chats: the creator, a shepherd, a released link
  and two moved copies (session-state audit 2026-10-29, ``t_e5cfb7c4``).
- A notice is a snapshot of the card when it was written. When the card leaves ``blocked``, its earlier
  "blocked" notice rows in the chat get ``display_metadata.stale = true`` (the Desktop dims them) and the
  card's id joins ``display_metadata.stale_tasks``; a row about several cards is stale once all of them moved on.
- :func:`card_change_pass` (run from the unloaded sweeper's loop, every backend) reads each board's new status
  events since the last pass and, for every chat linked to a changed card, marks those notices and refreshes the
  chat's stored outcome (trigger ``card_change``). The cursors are host-durable (``notice_fanout_state``), so the
  pass resumes where the last process stopped when the host loop lease moves it (design t_01a1d45d75851804 R3);
  only the install's very first pass seeds them. Every step is idempotent, so two backends running the pass at
  once (lease off) write the same result.
- Remote links have no local events. Each pass also snapshots the shared remote-card projection by chat and
  (owner-qualified key, status), durably like the cursors. The first successful read ever seeds it; additions,
  removals and status changes refresh the affected chats once each. A failed read preserves the previous
  snapshot. This refresh does not synthesize notices or change their stale metadata; normal remote notice
  delivery owns those rows.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from tui_gateway.notice_fanout_result import FanoutResult

logger = logging.getLogger(__name__)

MOVED_PREFIX = "moved_to_gateway:"
# Task events that can change a card's status (and so a linked chat's outcome). Chatter (heartbeats, comments,
# attachments, model tiers) is not read: thousands a day.
STATUS_EVENT_KINDS = (
    "created", "blocked", "unblocked", "scheduled", "promoted", "promoted_manual", "claimed", "reclaimed",
    "spawned", "completed", "archived", "gave_up", "crashed", "timed_out", "review_requested",
    "changes_requested", "review_reopened", "reopened", "status", "dependency_wait", "triage_swept", "moved",
    "continued", "continuation_woken", "condition_met",
)
MAX_EVENTS_PER_PASS = 2000
_BLOCKED_LINE = re.compile(r"Kanban (t_[0-9A-Za-z_]+) blocked")
_TASK_ID = re.compile(r"Kanban (t_[0-9A-Za-z_]+)")


def _reset_for_tests() -> None:
    global _remote_memo
    _remote_memo = None
    from tui_gateway import notice_fanout_state
    notice_fanout_state.reset_for_tests()


def chat_skip_reason(db: Any, chat_id: str) -> Optional[str]:
    """``"archived"`` / ``"moved"`` when no notice or refresh should reach ``chat_id`` here, else None. The row
    and its compression tip are both checked (archive flips a whole lineage; a move ends the tip)."""
    ids = list(dict.fromkeys(i for i in (chat_id, db.get_compression_tip(chat_id)) if i))
    marks = ",".join("?" * len(ids))
    for row in db._read_all(f"SELECT archived, end_reason FROM sessions WHERE id IN ({marks})", tuple(ids)):
        if str(row["end_reason"] or "").startswith(MOVED_PREFIX):
            return "moved"
        if row["archived"]:
            return "archived"
    return None


WORKER_TRANSCRIPT = "worker_transcript"


def worker_transcript_reason(db: Any, chat_id: str) -> Optional[str]:
    """``"worker_transcript"`` when ``chat_id`` is a headless run, not a chat: a ``local-<sha256>`` receipt id
    (Kanban worker, cron, one-shot) or a session this store tags ``kanban`` (older worker ids look like chats).
    A notice there is never read and its wake is always refused (``session.resume`` code 4132). [daveey]

    Hub card t_a989e5e0 (2026-10-31): five MacBook worker transcripts of another card held shepherd subs on it, so
    its done notice was written into all five and each wake was refused. Never raises (unknown = a chat)."""
    from hermes_cli.kanban_session_provenance import _HEADLESS_SESSION, _INTERACTIVE_SOURCES, session_creation_source
    from hermes_cli.kanban_session_provenance import MACHINE_SESSION_SOURCES
    # The supplied store is the destination authority, not the process's ambient profile.
    read = getattr(db, "get_session", None)
    try:
        row = read(chat_id) if read is not None else None
    except sqlite3.Error:
        row = None
    source = session_creation_source(dict(row)) if isinstance(row, (dict, sqlite3.Row)) else None
    if source in MACHINE_SESSION_SOURCES or (
            _HEADLESS_SESSION.fullmatch(chat_id) and source not in _INTERACTIVE_SOURCES):
        return WORKER_TRANSCRIPT
    probe = getattr(db, "is_kanban_owned_session", None)
    try:
        return WORKER_TRANSCRIPT if probe is not None and probe(chat_id) is True else None
    except sqlite3.Error:
        logger.debug("worker-transcript probe failed for %s", chat_id, exc_info=True)
        return None


def link_released(link: Any) -> bool:
    """A replica ``sessions[]`` link (or ``task_chat_links`` row) the owner released: no notices go there."""
    return isinstance(link, dict) and str(link.get("released") or "0").lower() not in ("0", "false", "")


def _decode(raw: Any) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _notice_tasks(content: str, meta: dict) -> set[str]:
    ids = {str(t) for t in (meta.get("task_ids") or ()) if t}
    if meta.get("task_id"):
        ids.add(str(meta["task_id"]))
    return ids | set(_TASK_ID.findall(content or ""))


def mark_stale_blocked_notices(db: Any, sid: str, task_id: str) -> int:
    """Mark the chat lineage's earlier "blocked" notices about ``task_id`` as outdated. Returns rows changed."""
    from hermes_state_sessions import _LINEAGE_CTE_SQL
    rows = db._read_all(
        _LINEAGE_CTE_SQL + " SELECT id, content, display_metadata FROM messages WHERE session_id IN "
        "(SELECT id FROM lineage) AND display_kind = 'kanban_notice' AND content LIKE ?",
        (sid, sid, f"%Kanban {task_id} blocked%"))
    changed = 0
    for row in rows:
        content = str(row["content"] or "")
        if task_id not in _BLOCKED_LINE.findall(content):
            continue
        meta = _decode(row["display_metadata"])
        stale_tasks = sorted({*(meta.get("stale_tasks") or ()), task_id})
        stale = set(stale_tasks) >= _notice_tasks(content, meta)
        if meta.get("stale_tasks") == stale_tasks and bool(meta.get("stale")) == stale:
            continue
        meta.update(stale_tasks=stale_tasks, **({"stale": True} if stale else {}))
        committed = db._write_rowcount("UPDATE messages SET display_metadata = ? WHERE id = ? AND display_metadata IS ?",
                                       (json.dumps(meta, ensure_ascii=False), row["id"], row["display_metadata"]))
        if not committed:
            # Another writer changed this row after capture. Replay from the durable
            # cursor, merging its metadata instead of acknowledging a lost update.
            raise RuntimeError("card-change stale metadata conflicted")
        changed += committed
    return changed


def _linked_chats(conn: sqlite3.Connection, task_id: str, created_by: Optional[str]) -> list[str]:
    """Chats linked to the card on this board: its creating chat and its ``tui`` subscribers, minus chats
    whose every ``task_chat_links`` row for the card is released."""
    chats = [created_by] if created_by else []
    chats += [str(r[0]) for r in conn.execute(
        "SELECT chat_id FROM kanban_notify_subs WHERE task_id = ? AND LOWER(platform) = 'tui' AND chat_id != ''",
        (task_id,))]
    released: set[str] = set()
    with contextlib.suppress(sqlite3.Error):  # boards that never synced have no link table
        rows = conn.execute("SELECT chat_session, MIN(released) FROM task_chat_links WHERE task_id = ? "
                            "GROUP BY chat_session", (task_id,)).fetchall()
        released = {str(r[0]) for r in rows if r[1]}
    return [c for c in dict.fromkeys(chats) if c not in released]


def _changed_tasks(conn: sqlite3.Connection, ident: str) -> tuple[list[str], int]:
    """Select work without acknowledging it; the caller commits actions before the cursor."""
    from tui_gateway import notice_fanout_state as state
    top = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0])
    since = state.get_cursor(ident, strict=True)
    if since is not None and top < since:
        # A replaced/truncated source cannot certify the previous action cursor.
        # Keep that cursor intact; neither a feed reset nor a quiet read ACKs loss.
        raise RuntimeError("card-change durable event source regressed")
    if since is None or top == since:
        return [], top
    kinds = ",".join("?" * len(STATUS_EVENT_KINDS))
    rows = conn.execute(f"SELECT task_id, id FROM task_events WHERE id > ? AND id <= ? AND kind IN ({kinds}) "
                        f"ORDER BY id ASC LIMIT ?", (since, top, *STATUS_EVENT_KINDS, MAX_EVENTS_PER_PASS)).fetchall()
    boundary = int(rows[-1][1]) if len(rows) == MAX_EVENTS_PER_PASS else top
    return list(dict.fromkeys(str(r[0]) for r in rows)), boundary


def _board_idle(ident: str) -> bool:
    """True when the board's newest ``task_events`` id equals the durable cursor: nothing happened since the last
    pass, so the per-pass board connect (schema prepare, 121 of the pass's 647 profile samples) and the fan-out are
    skipped. Read through the shared retained reader. The cursor IS the effective key (a changed board, or any
    unknown (no cursor, unreadable board), runs the full pass, which seeds or advances it). [daveey]"""
    from hermes_cli.kanban_reader_retention import reader
    from tui_gateway import notice_fanout_state as state
    try:
        since = state.get_cursor(ident, strict=True)
        if since is None:
            return False
        with reader(Path(ident)) as ro:
            top = int(ro.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0])
            if top == since:
                return True
            if top < since:
                return False
            # Heartbeats, comments and other bookkeeping events move the newest id every few seconds on a live
            # board, but ``_changed_tasks`` fans out only STATUS_EVENT_KINDS: when none lies in (since, top] the
            # full pass would return no card, and only advance the cursor, which is all this does (t_01a1dfa94f2251cd).
            kinds = ",".join("?" * len(STATUS_EVENT_KINDS))
            if ro.execute(f"SELECT 1 FROM task_events WHERE id > ? AND id <= ? AND kind IN ({kinds}) LIMIT 1",
                          (since, top, *STATUS_EVENT_KINDS)).fetchone() is not None:
                return False
        return state.set_cursor(ident, top)
    except Exception:  # health: allow BLE001 -- an unreadable board is not idle; the full pass decides
        logger.debug("card-change idle check unavailable for %s", ident, exc_info=True)
        return False


def apply_card_change(db: Any, tip: str, chat_id: str, task_id: str, status: str) -> Optional[str]:
    """One linked chat's share of a card change: stale notices when the card is not blocked, then a refresh.
    Returns why the chat was skipped, else None."""
    from hermes_state_outcome import refresh_outcomes_quietly
    if (why := chat_skip_reason(db, chat_id)) is not None:
        return why
    if status != "blocked":
        with contextlib.suppress(Exception):
            mark_stale_blocked_notices(db, tip, task_id)
    refresh_outcomes_quietly([tip], "card_change", db=db)
    return None


def _fan_out_changes(conn: sqlite3.Connection, _kb: Any, session_db: Callable, slug: str, tids: list[str]) -> list[dict]:
    """Every linked chat's share of this pass's changed cards on one board. A chat linked to several changed cards
    is opened once: each card marks its stale notices, then the outcome is refreshed ONCE (it derives from current
    state, so one refresh after all marks equals one per card; a busy hub's shepherd chat was refreshed per card,
    the dominant cost of the pass: H2 profile, 2026-11-15)."""
    by_chat: dict[str, list[tuple[str, str]]] = {}
    for tid in tids:
        task = _kb.get_task(conn, tid)
        status = str(getattr(task, "status", "") or "")
        for chat in _linked_chats(conn, tid, getattr(task, "session_id", None)):
            by_chat.setdefault(chat, []).append((tid, status))
    out = []
    for chat, cards in by_chat.items():
        with session_db(chat) as (db, tip):
            if db is None:
                raise RuntimeError("card-change session store unavailable")
            skipped = chat_skip_reason(db, chat)
            if skipped is None:
                for tid, status in cards:
                    if status != "blocked":
                        mark_stale_blocked_notices(db, tip, tid)
                from hermes_state_outcome import refresh_outcomes_quietly
                if not refresh_outcomes_quietly([tip], "card_change", db=db):
                    raise RuntimeError("card-change outcome not committed")
        out += [{"board": slug, "task_id": tid, "chat_id": chat, "session_id": tip, "status": status,
                 "skipped": skipped} for tid, status in cards]
    return out


# What the last remote pass that left every chat acknowledged was computed over: ``(base, ledger digest, {board:
# per-task change cursor}, {board: task ids the pass projected})``. A pass whose projection inputs are unchanged reads
# the same snapshot.
_remote_memo: Optional[tuple] = None


def _remote_inputs_key() -> Optional[tuple]:
    """``(base, ledger cards)`` of everything ``activity._remote_cards`` reads for the replica projection that
    ``_remote_change_pass`` keeps (card key, status, chat, origin chat), or None when any of it cannot be vouched
    for: this gateway and profile, the synced boards, and the origin-ledger projection's cards. The replica's own
    content is judged per board by ``_remote_boards_quiet`` (a pull that moves only ``pulled_at``, or writes a task
    no linked chat of this origin names, is not a change: t_01a1dfa94f2251cd)."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli.profiles import get_active_profile_name

    if sync.sync_config().get("links_reader") != "replica":
        return None
    try:
        gateway = ksc.load_identity().gateway
    except ksc.SyncUnavailable:
        return None
    if not gateway:
        return None
    boards = tuple((slug, str(ksc.shadow_path(slug))) for slug, _path in ksc.board_paths())
    return (gateway, get_active_profile_name(), boards), activity._remote_links(stale_ok=False)


# The ledger-card fields ``kanban_chat_links.replica_cards`` reads (owner labels, profile-board cards, cold-start
# fallback) and the pass keeps (key, status, chat, origin chat, unfinished).
_LEDGER_KEY_FIELDS = ("key", "board", "id", "gateway", "profile", "session", "origin_session", "origin_gateway",
                      "origin_profile", "status", "unfinished", "released", "ownership_state")


def _ledger_content_key(cards: list[dict]) -> str:
    """A digest of what the pass can see of the origin-ledger projection. By content, not object identity: a warm
    ``stale_ok`` read re-projects in the background once its entry is older than ``_LINKS_MAX_AGE_S`` (15 s), so
    every 30 s pass would see new dicts for an unchanged ledger (t_01a1e16c9e6427ce)."""
    rows = sorted(repr(tuple(c.get(f) for f in _LEDGER_KEY_FIELDS)) for c in cards)
    return hashlib.blake2b("\n".join(rows).encode(), digest_size=16).hexdigest()


def _poll_boards(base: tuple, previous: Optional[dict]) -> Optional[dict]:
    """``{board: (task ids changed since the memo's cursor or FULL, next cursor)}`` over the synced replicas, or
    None when any is unreadable or cannot vouch for a cursor (the per-task change table)."""
    from hermes_cli.kanban_activity_revision import SHADOW_TABLES
    from hermes_cli.kanban_session_activity import cached_ro
    from hermes_cli.kanban_task_change import changed_since

    out = {}
    for slug, path in base[2]:
        try:
            with cached_ro(Path(path), busy_budget=0) as conn:
                changed, cursor = changed_since(conn, (previous or {}).get(slug), tables=SHADOW_TABLES)
        except Exception:  # health: allow BLE001 -- unknown is hot
            return None
        if cursor is None:
            return None
        out[slug] = (changed, cursor)
    return out


def _task_linked(conn: sqlite3.Connection, ids: list, gateway: str, profile: str) -> bool:
    """Any of these tasks has a chat link of this origin (released or not): a link written, moved or dropped."""
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ",".join("?" * len(chunk))
        if conn.execute(f"SELECT 1 FROM task_chat_links WHERE task_id IN ({marks}) AND chat_gateway = ? "
                        "AND chat_profile = ? LIMIT 1", (*chunk, gateway, profile)).fetchone() is not None:
            return True
    return False


def _task_prints(conn: sqlite3.Connection, ids: list) -> dict:
    """``{task id: (status, its chat links)}`` for these tasks (absent tasks are absent)."""
    out: dict = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for tid, status, owner in conn.execute(f"SELECT id, status, created_on FROM tasks WHERE id IN ({marks})", chunk):
            out[tid] = [(status, owner), []]
        for row in conn.execute(f"SELECT task_id, chat_gateway, chat_profile, chat_session, kind, released "
                                f"FROM task_chat_links WHERE task_id IN ({marks})", chunk):
            if row[0] in out:
                out[row[0]][1].append(tuple(row[1:]))
    return {tid: (v[0], tuple(sorted(map(repr, v[1])))) for tid, v in out.items()}


def _known_prints(base: tuple, known: dict) -> Optional[dict]:
    """``{board: {task id: print}}`` for the projected cards, or None when a board cannot be read."""
    from hermes_cli.kanban_session_activity import cached_ro
    out = {}
    for slug, path in base[2]:
        try:
            with cached_ro(Path(path), busy_budget=0) as conn:
                ids = sorted(known.get(slug, ()))
                prints = _task_prints(conn, ids)
                out[slug] = {tid: prints.get(tid) for tid in ids}
        except Exception:  # health: allow BLE001 -- unknown is hot
            return None
    return out


def _remote_boards_quiet(base: tuple, polled: dict, known: dict) -> bool:
    """True when every task written since the memo is a projected card whose status and chat links are what the
    projection saw (a heartbeat, a comment, a run row), or a task no chat of this origin links."""
    from hermes_cli.kanban_session_activity import cached_ro
    from hermes_cli.kanban_task_change import FULL

    for slug, path in base[2]:
        changed = polled[slug][0]
        if changed is FULL:
            return False
        if not changed:
            continue
        seen = known.get(slug, {})
        try:
            with cached_ro(Path(path), busy_budget=0) as conn:
                mine = sorted(changed & seen.keys())
                if mine:
                    now = _task_prints(conn, mine)
                    if any(now.get(tid) != seen[tid] for tid in mine):
                        return False
                rest = sorted(changed - seen.keys())
                if rest and _task_linked(conn, rest, base[0], base[1]):
                    return False
        except Exception:  # health: allow BLE001 -- unknown is hot
            return False
    return True


def _remote_change_pass(session_db: Callable, origin_db: Optional[Callable] = None) -> FanoutResult:
    """Observe the same remote projection as outcomes; failed reads never mean cards finished."""
    global _remote_memo
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli import fact_links_health
    from tui_gateway import notice_links_health
    origin = activity._origin_scope()
    health_generation = fact_links_health.pending(origin)
    out = FanoutResult()
    try:
        inputs = _remote_inputs_key()
    except Exception:  # health: allow BLE001 -- an unreadable input is unknown: run the pass
        logger.debug("remote card-change inputs unavailable", exc_info=True)
        out.retry()
        inputs = None
    polled = None
    if inputs is not None:
        try:
            memo = _remote_memo
            polled = _poll_boards(inputs[0], memo[2] if memo is not None and memo[0] == inputs[0] else None)
        except Exception:  # health: allow BLE001 -- unknown is hot
            logger.debug("remote card-change boards unavailable", exc_info=True)
            polled = None
        if polled is None:
            out.retry()
            inputs = None
    memo = _remote_memo
    if health_generation is None and inputs is not None and memo is not None and memo[0] == inputs[0] and memo[1] == _ledger_content_key(inputs[1]) \
            and _remote_boards_quiet(inputs[0], polled, memo[3]):
        _remote_memo = (memo[0], memo[1], {slug: polled[slug][1] for slug in polled}, memo[3])
        return out
    _remote_memo = None
    from hermes_state_outcome import refresh_outcomes_quietly
    from tui_gateway import notice_fanout_state as state
    from hermes_cli.kanban_session_activity_release import PENDING_RELEASE_STATUSES
    current: dict[str, set[tuple[str, str]]] = {}
    known: dict[str, set[str]] = {}
    for card in activity._remote_cards(None, activity.LISTED_STATUSES, stale_ok=False):
        known.setdefault(str(card.get("board")), set()).add(str(card.get("id")))
        status = card.get("status")
        if status not in activity.LISTED_STATUSES and not (status in PENDING_RELEASE_STATUSES and card.get("unfinished")):
            continue
        for chat in {card.get("session"), card.get("origin_session")} - {None, ""}:
            current.setdefault(chat, set()).add((card["key"], status))
    snapshot = {chat: frozenset(cards) for chat, cards in current.items()}
    previous = state.load_remote(strict=True)
    if previous is None:
        if not state.seed_remote(snapshot):
            out.retry()
            return out
        if health_generation is None:
            return out
        previous = {}  # first source publication must also repair retained unknown outcomes
    acked: dict[str, Optional[frozenset[tuple[str, str]]]] = {}
    for chat in sorted(previous.keys() | snapshot.keys()):
        if health_generation is None and previous.get(chat) == snapshot.get(chat):
            continue
        try:
            with session_db(chat) as (db, tip):
                if db is None:
                    out.retry()
                    continue
                skipped = chat_skip_reason(db, chat)
                if skipped is None and not refresh_outcomes_quietly([tip], "card_change", db=db):
                    out.retry()
                    continue
        except Exception:  # health: allow BLE001 -- retain failed chat invalidations and continue other chats
            out.retry()
            logger.warning("remote card-change outcome failed for chat %s", chat, exc_info=True)
            continue
        # Acknowledge only a successful refresh or an intentionally skipped chat.
        acked[chat] = snapshot.get(chat)
        out.append({"board": None, "task_id": None, "chat_id": chat, "session_id": tip,
                    "status": None, "skipped": skipped, "source": "remote"})
    committed = state.ack_remote(acked)
    if not committed:
        out.retry()
    recovered_all = notice_links_health.recover_into(out, origin, health_generation, set(previous.keys() | snapshot.keys()), origin_db)
    if committed and inputs is not None and all(previous.get(c) == snapshot.get(c) or c in acked for c in previous.keys() | snapshot.keys()):
        # Only certify a projection if its cursor stayed still THROUGH fingerprint collection. A later
        # fingerprint must not describe a write the projection never saw.
        prints = _known_prints(inputs[0], known)
        after = _poll_boards(inputs[0], {slug: polled[slug][1] for slug in polled})
        if prints is not None and after is not None and all(after[slug][1] == polled[slug][1] for slug in polled):
            _remote_memo = (inputs[0], _ledger_content_key(inputs[1]), {slug: polled[slug][1] for slug in polled}, prints)
    fact_links_health.acknowledge(origin, health_generation,
                                  complete=committed and recovered_all and (previous.keys() | snapshot.keys()) <= acked.keys())
    return out


def _board_slugs(kb: Any) -> list[str]:
    """Enumerate without turning an unavailable roster into an empty success."""
    unique: dict[str, str] = {}
    for meta in kb.list_boards(include_archived=False):
        slug = (meta or {}).get("slug") or kb.DEFAULT_BOARD
        path = (meta or {}).get("db_path")
        ident = str(Path(path).expanduser().resolve() if path else kb.kanban_db_path(slug).resolve())
        unique.setdefault(ident, slug)
    return list(unique.values())


def card_change_pass(*, session_db: Optional[Callable] = None, slugs: Optional[Iterable[str]] = None) -> FanoutResult:
    """Refresh linked chats, returning the legacy row list plus scheduling state.

    ``FanoutResult.quiet`` certifies captured work drained; ``incomplete`` and
    ``next_deadline`` (absolute monotonic seconds) retain bounded retries even
    for an empty result. Callers must not infer quiet from list truthiness.
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    from tui_gateway import kanban_unloaded as ku
    session_db = session_db or ku.find_session_db
    done = FanoutResult()
    try:
        boards = list(slugs) if slugs is not None else _board_slugs(_kb)
    except Exception:  # health: allow BLE001 -- roster failure is an explicit outstanding action obligation
        logger.warning("card-change board enumeration failed", exc_info=True)
        boards = []
        done.retry()
    for slug in boards:
        try:
            ident = str(_kb.kanban_db_path(slug).resolve())
            if _board_idle(ident):
                continue
            with contextlib.closing(_kbc.connect(board=slug)) as conn:
                from tui_gateway import notice_fanout_state as state
                tids, cursor = _changed_tasks(conn, ident)
                done += _fan_out_changes(conn, _kb, session_db, slug, tids)
                if not state.set_cursor(ident, cursor):
                    done.retry()
                top = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0])
                if top > cursor:
                    done.retry(0.0)
        except Exception:
            done.retry()
            logger.warning("card-change outcome pass failed on board %s", slug, exc_info=True)
    try:
        done.absorb(_remote_change_pass(session_db))
    except Exception:  # health: allow BLE001 -- isolate the replica source and report failed reads
        done.retry()
        logger.warning("remote card-change outcome pass failed", exc_info=True)
    return done
