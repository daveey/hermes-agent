"""Own-board and sync-shadow content of the annotation holder's narrow key, by origin. [daveey]

``kanban_annotation_narrow`` used to hash every own board's and sync shadow's content key (``kanban_activity_revision``)
verbatim. Those revisions move on any non-bookkeeping commit anywhere in the file: a run row, a ``spawned`` event,
a pulled event on the shadow. On hermes-hub the default shadow moved on every 10 s sample and the default board on
about half of them, so all 8 demanded scopes' narrow keys moved on nearly every holder probe and each scope refreshed
every 12 s (floor + probe) instead of every 20 s renewal (t_01a1db1387f61a98, P1 at 17.9% vs a 2.4% budget).

This module keys on what one origin's annotation reads from those files instead:

* **Own boards** (status and membership). Per board, the open cards (status not ``done``/``archived``) a session of
  this origin profile owns or shepherds: ``tasks.session_id``, or a ``tui`` notify sub's ``chat_id`` (with whether
  that sub counts, ``_counted_sub_clause``), and for each: status, the needs-human verdict, ``block_kind``, the
  ``human_request`` text digest and the watch-park flag; plus every open ancestor of those cards over ``task_links``
  with its status and needs-human verdict (``_parked_on``'s held/parked test). "This origin profile's sessions" are
  the candidates found in this profile's ``state.db``. Claims, leases, heartbeats, run rows, comments and events of
  other kinds do not enter. A card leaving the open set (done/archived) moves the key.
* **Sync shadows** (linked rows). This origin's unreleased ``task_chat_links`` rows joined to their card rows
  (``kanban_chat_links.replica_link_rows``, the same rows ``replica_cards`` reads, from the same memo), with the
  replica's liveness field ``lease_expires`` dropped, and the replica's stale verdict at now (``pulled_at`` against
  ``_stale_after_s``), never the ``pulled_at`` clock.

Display-only state these omit (unseen-notice cursors, run liveness and execution evidence, the 24 h attention
window, stall verdicts, done receipts aging out) is bounded by the holder's 20 s forced renewal, like everything
else the narrow key misses. Per-board reads are memoised on the board's content key (shared by every scope in the
process), so a board commit is read once. Any read error raises; the caller returns None (= moved).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

FINISHED = ("done", "archived")
_SHADOW_DROP = frozenset({"lease_expires"})
_lock = threading.Lock()
_board_memo: dict[str, tuple] = {}  # board path -> (content key, change cursor or None, board rows)
_sessions_memo: dict[str, tuple] = {}  # state.db path -> (file ident, negatives since, found, checked)
_shadow_digests: dict[tuple, tuple] = {}  # (shadow path, profile) -> (link rows object, digest)
stats = {"board_reads": 0, "board_patches": 0, "session_reads": 0}
_PATCH_MAX = 400  # changed tasks above this: one full read is cheaper than the IN lists


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False,
                                     default=str).encode()).hexdigest()


def _chunks(ids: list[str]):
    for i in range(0, len(ids), 500):
        yield ids[i:i + 500]


def _board_rows(conn: sqlite3.Connection, ids: frozenset | None = None) -> dict:
    """Membership and status inputs of every open card on one board (``ids``: only those cards, and every link
    touching them), on ``conn``'s current read transaction. ``edges`` are raw ``(parent, child)`` links; finished
    ends are dropped where the key is built (``_board_part``)."""
    from hermes_cli.kanban_session_activity import _counted_sub_clause, _not_watch
    from hermes_cli.kanban_session_wait_rows import _stored_needs_human

    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "session_id" not in cols:
        return {"tasks": {}, "subs": {}, "edges": set()}  # pre-session schema: the counts read nothing here
    request = "t.human_request" if "human_request" in cols else "NULL"
    kind = "t.block_kind" if "block_kind" in cols else "NULL"
    finished = ",".join("?" * len(FINISHED))
    groups = [(None, ())] if ids is None else [(c, c) for c in _chunks(sorted(ids))]
    tasks: dict[str, list] = {}
    subs: dict[str, list] = {}
    edges: set[tuple[str, str]] = set()
    for chunk, args in groups:
        only = "" if chunk is None else f" AND t.id IN ({','.join('?' * len(chunk))})"
        for tid, status, sid, hr, bk, not_watch in conn.execute(
                f"SELECT t.id, t.status, t.session_id, {request}, {kind}, ({_not_watch(conn)}) FROM tasks t "
                f"WHERE t.status NOT IN ({finished}){only}", (*FINISHED, *args)):
            tasks[str(tid)] = [str(status), str(sid) if sid else None, bool(_stored_needs_human(hr, bk)),
                               bk, hashlib.sha256(str(hr).encode()).hexdigest()[:16] if hr is not None else None,
                               not bool(not_watch)]
        try:
            for tid, chat, counted in conn.execute(
                    f"SELECT s.task_id, s.chat_id, ({_counted_sub_clause(conn)}) FROM kanban_notify_subs s "
                    f"JOIN tasks t ON t.id = s.task_id WHERE s.platform = 'tui' AND s.chat_id != '' "
                    f"AND t.status NOT IN ({finished}){only}", (*FINISHED, *args)):
                subs.setdefault(str(tid), []).append([str(tid), str(chat), bool(counted)])
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
        try:
            where = "" if chunk is None else (f" WHERE parent_id IN ({','.join('?' * len(chunk))}) "
                                              f"OR child_id IN ({','.join('?' * len(chunk))})")
            edges.update((str(p), str(c)) for p, c in conn.execute(
                f"SELECT parent_id, child_id FROM task_links{where}", (*args, *args)))
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
    return {"tasks": tasks, "subs": {k: sorted(v) for k, v in subs.items()}, "edges": edges}


def _patched(rows: dict, ids: frozenset, fresh: dict) -> dict:
    """``rows`` with every card in ``ids`` (and every link touching one) replaced by ``fresh``; ``rows`` itself is
    left alone (other scopes may hold it)."""
    tasks = {k: v for k, v in rows["tasks"].items() if k not in ids}
    tasks.update(fresh["tasks"])
    subs = {k: v for k, v in rows["subs"].items() if k not in ids}
    subs.update(fresh["subs"])
    edges = {e for e in rows["edges"] if e[0] not in ids and e[1] not in ids} | fresh["edges"]
    return {"tasks": tasks, "subs": subs, "edges": edges}


def board_rows(path: Path, content: Any) -> dict:
    """``_board_rows`` of ``path``, memoised on its content key when that key is a trigger-backed revision. A moved
    revision re-reads only the cards its per-task change cursor names (``kanban_task_change``), or the whole board
    when the cursor cannot vouch for the gap."""
    from hermes_cli import kanban_task_change as change
    from hermes_cli.kanban_activity_revision import LOCAL_TABLES, is_revision
    from hermes_cli.kanban_session_activity import cached_ro

    revision = isinstance(content, tuple) and is_revision(content)
    with _lock:
        hit = _board_memo.get(str(path))
    if revision and hit is not None and hit[0] == content:
        return hit[2]
    with cached_ro(path, busy_budget=0) as conn:
        conn.execute("BEGIN")
        try:
            changed, cursor = change.changed_since(conn, hit[1] if hit is not None else None, tables=LOCAL_TABLES)
            if hit is not None and isinstance(changed, frozenset) and len(changed) <= _PATCH_MAX:
                rows = _patched(hit[2], changed, _board_rows(conn, changed)) if changed else hit[2]
                stats["board_patches"] += 1
            else:
                rows = _board_rows(conn)
                stats["board_reads"] += 1
        finally:
            conn.rollback()
    with _lock:
        # The cursor describes the snapshot just read, whatever ``content`` (read a moment earlier) says; a revision
        # content key still gates reuse, so a stat key never answers a later call from this memo.
        _board_memo[str(path)] = (content if revision else None, cursor, rows)
    return rows


_SESSION_NEGATIVE_TTL_S = 300.0


def profile_sessions(candidates: set[str]) -> set[str]:
    """The ``candidates`` that are sessions of this origin profile (its ``state.db``). A missing state.db has none.
    Only ids not yet looked up are read: a session is never re-homed to another profile, so a found id stays found
    (deletion is rare); an id not found is looked up again after ``_SESSION_NEGATIVE_TTL_S`` (a card can name a chat
    before its session row lands; the holder's forced renewal covers that window)."""
    from hermes_constants import get_hermes_home
    from hermes_cli.kanban_session_activity import cached_ro

    db = get_hermes_home() / "state.db"
    if not candidates:
        return set()
    try:
        st = db.stat()
    except FileNotFoundError:
        return set()
    ident, now = (st.st_dev, st.st_ino), time.monotonic()
    with _lock:
        hit = _sessions_memo.get(str(db))
    if hit is None or hit[0] != ident or now - hit[1] > _SESSION_NEGATIVE_TTL_S:
        hit = (ident, now, frozenset(), frozenset())  # (file, negatives since, found, checked)
    found, checked = set(hit[2]), set(hit[3])
    ids = sorted(set(candidates) - checked)
    if ids:
        with cached_ro(db, busy_budget=0) as conn:
            for chunk in _chunks(ids):
                found.update(str(r[0]) for r in conn.execute(
                    f"SELECT id FROM sessions WHERE id IN ({','.join('?' * len(chunk))})", chunk))
        checked.update(ids)
        stats["session_reads"] += 1
        with _lock:
            _sessions_memo[str(db)] = (ident, hit[1], frozenset(found), frozenset(checked))
            while len(_sessions_memo) > 64:
                del _sessions_memo[next(iter(_sessions_memo))]
    return found & set(candidates)


def _parents(rows: dict) -> dict[str, list[str]]:
    """``child -> [open parents]`` of ``rows``, built once per rows object (rows are never mutated once built)."""
    index = rows.get("_parents")
    if index is None:
        tasks, out = rows["tasks"], {}
        for parent, child in rows["edges"]:
            if child in tasks and parent in tasks:  # finished/unknown parents never wait
                out.setdefault(child, []).append(parent)
        index = rows["_parents"] = {k: sorted(v) for k, v in out.items()}
    return index


def _candidates(rows: dict) -> frozenset:
    found = rows.get("_candidates")
    if found is None:
        found = rows["_candidates"] = frozenset(
            {t[1] for t in rows["tasks"].values() if t[1]} | {s[1] for v in rows["subs"].values() for s in v})
    return found


def _board_part(rows: dict, mine: frozenset) -> list:
    """The cards of ``rows`` this origin's sessions own or shepherd, plus their open ancestors (memoised per rows
    object and session set: every scope of the process shares one board read)."""
    parts = rows.setdefault("_parts", {})
    hit = parts.get(mine)
    if hit is not None:
        return hit
    tasks, parents = rows["tasks"], _parents(rows)
    subs = sorted(s for v in rows["subs"].values() for s in v if s[1] in mine)
    own = {tid for tid, t in tasks.items() if t[1] in mine} | {s[0] for s in subs}
    cards = [[tid, *tasks[tid][:1], tasks[tid][1] if tasks[tid][1] in mine else None, *tasks[tid][2:],
              parents.get(tid, [])] for tid in sorted(own) if tid in tasks]
    seen: set[str] = set()
    stack = [p for tid in own for p in parents.get(tid, ())]
    while stack:
        p = stack.pop()
        if p not in seen and p in tasks:
            seen.add(p)
            stack.extend(parents.get(p, ()))
    ancestors = [[p, tasks[p][0], tasks[p][2], parents.get(p, [])] for p in sorted(seen - own)]
    out = [cards, subs, ancestors]
    if len(parts) > 16:
        parts.clear()
    parts[mine] = out
    return out


def boards_part(entries: list[tuple[str, Any]]) -> list:
    """Own-board component for ``entries`` (``(board path, content key)`` from the exact fingerprint)."""
    reads = [(path, board_rows(Path(path), content)) for path, content in entries]
    candidates = set().union(*(_candidates(rows) for _, rows in reads)) if reads else set()
    mine = frozenset(profile_sessions(candidates))
    return [[path, _board_part(rows, mine)] for path, rows in reads]


def _shadow_gateway() -> str | None:
    from hermes_cli import kanban_sync_client as ksc
    try:
        return ksc.load_identity().gateway or None
    except ksc.SyncUnavailable:
        return None


def shadows_part(entries: list[tuple[str, Any]], profile: str, now: float | None = None) -> list:
    """Sync-shadow component: per shadow, this origin's linked rows (liveness dropped) and its stale verdict."""
    from hermes_cli import kanban_chat_links as links
    from hermes_cli.kanban_session_activity import cached_ro

    if not entries:
        return []
    gateway = _shadow_gateway()
    if not gateway:
        return [["no-gateway", [path for path, _ in entries]]]
    now = time.time() if now is None else now
    stale_after = links._stale_after_s()
    out = []
    for path, content in entries:
        with cached_ro(Path(path), busy_budget=0) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = links.replica_link_rows(conn, Path(path), gateway, profile, content)
                try:
                    row = conn.execute("SELECT pulled_at FROM kanban_sync_state LIMIT 1").fetchone()
                    pulled = float(row[0]) if row and row[0] is not None else None
                except sqlite3.OperationalError:  # replica written before pulls were stamped
                    pulled = None
            finally:
                conn.row_factory = None
        with _lock:
            memo = _shadow_digests.get((path, profile))
        if memo is not None and memo[0] is rows:
            body = memo[1]
        else:
            body = _digest(sorted(_digest({k: v for k, v in r.items() if k not in _SHADOW_DROP}) for r in rows))
            with _lock:
                _shadow_digests[(path, profile)] = (rows, body)
                while len(_shadow_digests) > 64:
                    del _shadow_digests[next(iter(_shadow_digests))]
        out.append([path, body, pulled is None or now - pulled > stale_after])
    return out


def local_part(local: list, profile: str, now: float | None = None) -> list:
    """Both components for the exact fingerprint's local entries (``(path, content key)``, boards then shadows).
    Raises ``ValueError`` on an entry of unknown shape."""
    from hermes_cli.kanban_activity_revision import is_shadow

    boards, shadows, missing = [], [], []
    for entry in local:
        if not (isinstance(entry, (tuple, list)) and len(entry) == 2 and isinstance(entry[0], str)):
            raise ValueError("unknown activity fingerprint entry")
        content = entry[1]
        if isinstance(content, (tuple, list)) and content[:1] in (("stat",), ["stat"]) and len(content) != 4:
            raise ValueError("unreadable board")  # unknown, never unchanged (``host._portable``'s rule)
        if not Path(entry[0]).is_file():
            missing.append(entry[0])  # no file reads nothing; its appearance moves the key
            continue
        (shadows if is_shadow(entry[0]) else boards).append((entry[0], content))
    return [boards_part(boards), shadows_part(shadows, profile, now), sorted(missing)]


def reset() -> None:
    with _lock:
        _board_memo.clear()
        _sessions_memo.clear()
        _shadow_digests.clear()
