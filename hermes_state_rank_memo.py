"""[daveey] Incremental ranked session ids (design t_01a1d563324d7e8e §3 D2, review R5, C9).

``/api/profiles/sessions``, the batched sidebar and ``/projects/tree`` all read
``list_sessions_rich(order_by_last_active=True)`` pages; the Desktop refreshes them on every
``sessions.changed`` (at most each 10 s), and each read re-ranked the store. This module keeps, per store
(``hermes_state_recency._StoreRecency``), a memo of RANKED PAGE IDS ONLY, keyed by the page's exact
filter (WHERE text, bound params, seed form) and its length. Rows are projected on every request.

Clock. The store keeps one clock: D1's session cursor (``hermes_state_session_change``), the
``messages.id`` watermark and the ``session_rank_lowered`` counter. Each request advances it inside its
own read snapshot and adds the ids that moved (changed sessions, sessions with new messages) to every
entry's ``dirty`` set. The clock is untrusted (cursor FULL/None, watermark not AUTOINCREMENT or moved
back, the lowered triggers not exactly installed): every entry and preview is dropped, the read takes the
exact path. The counter moved (a message deleted, (de)activated by a rewind or compaction, or its
``timestamp``/``session_id`` rewritten): same.

Re-rank. An entry with ``dirty`` re-ranks ``R`` = previous page ∪ dirty ∪ every ancestor of dirty (chain
roots: a member's change moves its root) ∪ the direct children of dirty (a child's admission reads its
parent) with the exact ranked query restricted to ``R`` (the chain walk itself is not restricted). If
every previous page row is still in the result with a sort key (effective_last_active, started_at, id;
SQLite's order) no lower than before, the new page is the top of ``R``. Proof: a row outside ``R`` was not
on the previous page, so it was not admitted or ranked strictly below the previous boundary, and none of
its inputs moved (its own row, its parent, its chain's rows and messages); the previous page rows alone
still fill the page at or above that boundary. Otherwise (a page row removed or lowered) the exact query
runs. A params change is a different key, so a fresh entry from the exact query. SQLite ranks only
dirty's relatives plus the page rows that had a child when the entry was put (``_rerank``; the triggers stamp
a deleted or re-parented member's own id, never its old parent): any other page row moved none of its
inputs, so the memo's sort key for it IS its exact one, and it is merged back in SQLite's order.

Previews. ``_preview_raw`` (the first eligible user message, ~95% of the page projection on the hub) is
cached per store as ``{session_id: (transcript_revision, raw)}`` and used in SQL only when the row's
``transcript_revision`` still matches (the ``messages_*_revision_update`` triggers bump it on a content,
role or ``display_kind`` rewrite; both must be exactly installed or the cache is off). An entry is dropped
when its session moves on the clock (a new message, or a session-row change: ``transcript_revision``
restarts at 0 on a re-insert), and the cache is cleared with the clock. A projection installs previews only
if the clock did not advance since its own snapshot advanced it (a slower, older reader never installs).

Assumptions shared with ``hermes_state_recency``: ``messages.id`` is AUTOINCREMENT and no writer inserts
an explicit old id or ``INSERT OR REPLACE``s a message (REPLACE deletes fire no DELETE trigger).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("hermes_state")

PREFIX = "session_rank_on_"
LOWERED_TABLE = "session_rank_lowered"
OUTCOMES: Counter = Counter()  # served / reranked / fallback/<reason> (tests, P1 via loop_accounting)

_MAX_ENTRIES = 64
_MAX_PREVIEWS = 8192
_MAX_PREVIEW_CHARS = 4096
_PREVIEW_TRIGGERS = {  # name -> columns its UPDATE OF list must name for the preview cache to be exact
    "messages_transcript_revision_update": ("role", "content"),
    "messages_display_revision_update": ("display_kind",),
}


# ── the lowered counter ───────────────────────────────────────────────────────

def _bump() -> str:
    return f"UPDATE {LOWERED_TABLE} SET n = n + 1 WHERE id = 1;"


def wanted_triggers(conn: sqlite3.Connection) -> dict[str, str]:
    """Trigger name -> CREATE statement. Message deletes, (de)activations and in-place moves can LOWER a
    session's rank (or its chain root's), which a page memo cannot see through the clock's other parts."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    if not {"id", "session_id", "timestamp"} <= columns:
        return {}
    out = {f"{PREFIX}messages_delete": f"CREATE TRIGGER {PREFIX}messages_delete AFTER DELETE ON messages "
                                       f"BEGIN {_bump()} END",
           f"{PREFIX}messages_move": (f"CREATE TRIGGER {PREFIX}messages_move AFTER UPDATE OF timestamp, session_id "
                                      "ON messages WHEN OLD.timestamp IS NOT NEW.timestamp OR "
                                      f"OLD.session_id IS NOT NEW.session_id BEGIN {_bump()} END")}
    if "active" in columns:
        out[f"{PREFIX}messages_active"] = (f"CREATE TRIGGER {PREFIX}messages_active AFTER UPDATE OF active ON "
                                           f"messages WHEN OLD.active IS NOT NEW.active BEGIN {_bump()} END")
    return out


def _installed(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name GLOB ?",
                             (PREFIX + "*",)))


def ensure_installed(conn: sqlite3.Connection) -> bool:
    """Install or repair the counter and its triggers; True when it wrote. Own prefix, so D1's
    ``ensure_installed`` (which drops unknown ``session_change_on_*`` triggers) never touches these."""
    wanted = wanted_triggers(conn)
    if not wanted:
        return False
    found = _installed(conn)
    has_row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                           (LOWERED_TABLE,)).fetchone() and conn.execute(
        f"SELECT 1 FROM {LOWERED_TABLE} WHERE id = 1").fetchone()
    if found == wanted and has_row:
        return False
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {LOWERED_TABLE} "
                     "(id INTEGER PRIMARY KEY CHECK (id = 1), n INTEGER NOT NULL)")
        conn.execute(f"INSERT OR IGNORE INTO {LOWERED_TABLE} (id, n) VALUES (1, 0)")
        # A (re)install counts as a lowering: no memo built across a gap in coverage is trusted.
        conn.execute(f"UPDATE {LOWERED_TABLE} SET n = n + 1 WHERE id = 1")
        for name in set(found) - set(wanted):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        for name, sql in wanted.items():
            if found.get(name) != sql:
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
                conn.execute(sql)
        if own:
            conn.execute("COMMIT")
    except BaseException:
        if own and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return True


def install_fail_open(conn: sqlite3.Connection) -> None:
    """``ensure_installed`` for the writer init: a failure never fails the open; memos fall back."""
    try:
        ensure_installed(conn)
    except sqlite3.Error as exc:
        logger.warning("session rank memo triggers not installed (pages use the exact ranking): %s", exc)


_COVERED: dict[tuple, tuple[bool, bool]] = {}  # (path, dev, ino, cookie) -> (lowered ok, preview ok)
_COVERED_LOCK = threading.Lock()


def _update_of(sql: str) -> set[str]:
    m = re.search(r"AFTER\s+UPDATE\s+OF\s+(.*?)\s+ON\s+messages", sql or "", re.IGNORECASE | re.DOTALL)
    return {c.strip().strip('"').lower() for c in m.group(1).split(",")} if m else set()


def _coverage(conn: sqlite3.Connection, key: tuple) -> tuple[bool, bool]:
    with _COVERED_LOCK:
        hit = _COVERED.get(key)
    if hit is not None:
        return hit
    wanted = wanted_triggers(conn)
    lowered = bool(wanted) and _installed(conn) == wanted and bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (LOWERED_TABLE,)).fetchone())
    found = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name IN (?, ?)",
                              tuple(_PREVIEW_TRIGGERS)))
    preview = all(set(cols) <= _update_of(found.get(name, "")) for name, cols in _PREVIEW_TRIGGERS.items())
    preview = preview and "transcript_revision" in {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    with _COVERED_LOCK:
        if len(_COVERED) >= 64:
            _COVERED.clear()
        _COVERED[key] = (lowered, preview)
    return lowered, preview


# ── sort keys in SQLite's order ───────────────────────────────────────────────

def _v(value: Any) -> tuple:
    """SQLite's cross-type order: NULL < numbers < TEXT (BINARY: UTF-8 bytes order = code point order) < BLOB."""
    if value is None:
        return (0, 0)
    if isinstance(value, (int, float)):
        return (1, value)
    if isinstance(value, str):
        return (2, value)
    return (3, bytes(value))


def sort_key(eff: Any, started_at: Any, sid: Any) -> tuple:
    return (_v(eff), _v(started_at), _v(sid))


# ── per-store memo ────────────────────────────────────────────────────────────

@dataclass
class _Entry:
    ids: list  # ranked page ids, best first
    keys: dict  # id -> sort key
    eff: dict  # id -> effective_last_active as SQLite returned it
    started: dict  # id -> started_at as SQLite returned it (rebuilds a kept row's sort key)
    rooted: set  # page ids that had a child at put time (``_rerank`` always re-ranks them)
    dirty: set = field(default_factory=set)


@dataclass
class _Clock:
    cursor: Any
    watermark: int
    lowered: int


class RankMemo:
    """Lives on ``_StoreRecency.rank_memo``; every method runs under ``_StoreRecency.lock`` except
    ``install_previews``/``preview_rows`` (they take it themselves)."""

    def __init__(self, lock: threading.Lock):
        self.lock = lock
        self.clock: Optional[_Clock] = None
        self.generation = 0
        self.entries: OrderedDict[tuple, _Entry] = OrderedDict()
        self.previews: OrderedDict[str, tuple] = OrderedDict()
        self.preview_ok = False
        from hermes_state_row_memo import RowMemo
        self.rows = RowMemo()  # [daveey] decoded projected rows (hermes_state_row_memo), same clock and lock

    def _reset(self) -> None:
        self.clock = None
        self.entries.clear()
        self.previews.clear()
        self.rows.clear()
        self.generation += 1

    def advance_for_test(self) -> None:
        with self.lock:
            self.generation += 1

    def advance(self, conn: sqlite3.Connection) -> Optional[str]:
        """Move the clock to this snapshot; None, or the fallback reason (every entry was dropped)."""
        import hermes_state_session_change as sc
        changed, cursor = sc.changed_since(conn, self.clock.cursor if self.clock else None)
        seq_row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'messages'").fetchone()
        max_id = conn.execute("SELECT MAX(id) FROM messages").fetchone()[0]
        path = sc._main_path(conn)
        ident = sc._identity(path)
        cookie = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        lowered_ok, self.preview_ok = _coverage(conn, (path, *(ident[:2] if ident else (None, None)), cookie))
        if cursor is None or (seq_row is None and max_id is not None) or not lowered_ok:
            self._reset()
            return "untrusted"
        watermark = max(int(seq_row[0]) if seq_row else 0, int(max_id or 0))
        lowered = int(conn.execute(f"SELECT n FROM {LOWERED_TABLE} WHERE id = 1").fetchone()[0])
        previous = self.clock
        if previous is None:
            self._reset()
            self.clock = _Clock(cursor, watermark, lowered)
            return None
        if changed is sc.FULL or watermark < previous.watermark:
            self._reset()
            self.clock = _Clock(cursor, watermark, lowered)
            return "untrusted"
        if lowered != previous.lowered:
            self._reset()
            self.clock = _Clock(cursor, watermark, lowered)
            return "lowered"
        moved = set(changed)
        if watermark > previous.watermark:
            moved |= {sid for (sid,) in conn.execute(
                "SELECT DISTINCT session_id FROM messages NOT INDEXED WHERE id > ? AND id <= ?",
                (previous.watermark, watermark)) if sid is not None}
        self.clock = _Clock(cursor, watermark, lowered)
        if moved:
            self.generation += 1
            for sid in moved:
                self.previews.pop(sid, None)
                self.rows.drop(sid)
            for key in list(self.entries):
                entry = self.entries[key]
                entry.dirty |= moved
                if len(entry.dirty) > max(256, 4 * len(entry.ids)):
                    del self.entries[key]
        return None

    def put(self, conn: sqlite3.Connection, key: tuple, rows: list) -> _Entry:
        ids = [r[0] for r in rows]
        rooted = set(json.loads(conn.execute(_ROOTED_SQL, (json.dumps(ids),)).fetchone()[0] or "[]"))
        entry = _Entry(ids, {r[0]: sort_key(r[3], r[1], r[0]) for r in rows},
                       {r[0]: r[3] for r in rows}, {r[0]: r[1] for r in rows}, rooted)
        self.entries[key] = entry
        self.entries.move_to_end(key)
        while len(self.entries) > _MAX_ENTRIES:
            self.entries.popitem(last=False)
        return entry

    def cached_previews(self, generation: int, ids: list) -> list:
        with self.lock:
            if generation != self.generation or not self.preview_ok:
                return []
            out = []
            for sid in ids:
                hit = self.previews.get(sid)
                if hit is not None:
                    self.previews.move_to_end(sid)
                    out.append([sid, hit[0], hit[1]])
            return out

    def install_previews(self, generation: int, values: dict) -> None:
        with self.lock:
            if generation != self.generation or not self.preview_ok:
                return  # the clock moved past this reader's snapshot
            for sid, (rev, raw) in values.items():
                if isinstance(raw, str) and len(raw) <= _MAX_PREVIEW_CHARS and rev is not None:
                    self.previews[sid] = (rev, raw)
                    self.previews.move_to_end(sid)
            while len(self.previews) > _MAX_PREVIEWS:
                self.previews.popitem(last=False)


# ── the page ──────────────────────────────────────────────────────────────────

_RELATIVES_SQL = """
    WITH RECURSIVE up(id) AS (
        SELECT value FROM json_each(?1)
        UNION SELECT s.parent_session_id FROM up JOIN sessions s ON s.id = up.id
        WHERE s.parent_session_id IS NOT NULL
    )
    SELECT id FROM up
    UNION SELECT c.id FROM sessions c WHERE c.parent_session_id IN (SELECT value FROM json_each(?1))"""

# The page ids with a child, as one JSON row: only these have chain members a write can take away.
_ROOTED_SQL = ("SELECT json_group_array(DISTINCT parent_session_id) FROM sessions "
               "WHERE parent_session_id IN (SELECT value FROM json_each(?))")

_IDS_HEAD = "SELECT s.id, s.started_at, "  # + last_active, _effective_last_active from ranked_query


def _account(outcome: str) -> None:
    OUTCOMES[outcome] += 1
    try:
        from hermes_cli import loop_accounting as la
        la.record("recency.rank_memo", skipped=outcome == "served", changed=outcome == "reranked",
                  fallback=outcome.startswith("fallback/"), outer=False)
        if outcome.startswith("fallback/"):
            la.record(f"recency.rank_memo.{outcome}", outer=False)
    except (ImportError, AttributeError, TypeError, ValueError):  # accounting never fails a read
        logger.debug("rank memo pass accounting failed", exc_info=True)


def _rerank(entry: _Entry, relatives: set, restricted_ids) -> list:
    """The exact ranking of ``R`` = previous page ∪ ``relatives``, best first, stepping SQLite only through
    ``relatives`` and the page rows that had a child (``entry.rooted``). Any other page row moved none of
    its inputs (module doc), so its sort key and effective_last_active are the memo's. ``relatives`` alone
    is not enough: the change triggers stamp only a row's own id, so a chain member deleted, re-parented or
    detached no longer leads back to the page row whose chain it left (that row's key can drop); every such
    row had a child when the entry was put, so it is in ``rooted``. Merged in SQLite's order (``sort_key``,
    a total order: ids are unique). Re-ranking the whole previous page brought every row of a 2000-row tree
    page back to Python on each refresh, under the store lock, for the few rows a write touched."""
    touched = relatives | entry.rooted
    fresh = list(restricted_ids(sorted(touched))) if touched else []
    kept = [(sid, entry.started[sid], None, entry.eff[sid]) for sid in entry.ids if sid not in touched]
    return sorted(kept + fresh, key=lambda r: sort_key(r[3], r[1], r[0]), reverse=True)


def ranked_ids(conn, memo: RankMemo, key: tuple, need: int, exact_ids, restricted_ids) -> list:
    """``[(id, started_at, last_active, effective_last_active)]`` of the top ``need`` rows, from the memo
    when provable, else ``exact_ids()``. ``restricted_ids(ids)`` is the exact ranking of ``ids`` alone.
    Called under the store lock inside the caller's read snapshot."""
    reason = memo.advance(conn)
    entry = memo.entries.get(key) if reason is None else None
    if entry is None:
        rows = exact_ids(invalidate_candidates=reason in ("lowered", "untrusted"))
        if memo.clock is not None:
            memo.put(conn, key, rows)
        _account(f"fallback/{reason or 'no_entry'}")
        return rows
    memo.entries.move_to_end(key)
    if not entry.dirty:
        _account("served")
        return [(sid, None, None, entry.eff[sid]) for sid in entry.ids]
    dirty = json.dumps(sorted(entry.dirty))
    relatives = {sid for (sid,) in conn.execute(_RELATIVES_SQL, (dirty,)) if sid is not None}
    rows = _rerank(entry, relatives, restricted_ids)
    got = {r[0]: sort_key(r[3], r[1], r[0]) for r in rows}
    for sid in entry.ids:
        if sid not in got:
            reason = "removed"
            break
        if got[sid] < entry.keys[sid]:
            reason = "rank_lowered"
            break
    if reason is None and len(entry.ids) >= need and len(rows) >= need and sorted(got.values())[-need] < min(
            entry.keys.values()):
        reason = "threshold"  # unreachable when the checks above hold; kept as a guard
    if reason is not None:
        del memo.entries[key]
        rows = exact_ids()
        memo.put(conn, key, rows)
        _account(f"fallback/{reason}")
        return rows
    rows = rows[:need]
    memo.put(conn, key, rows)
    _account("reranked")
    return rows


def project(conn, memo: RankMemo, ids_rows: list, select_head: str, prompt_join: str, preview_sql: str,
            last_active_sql: str, generation: int) -> list:
    """The page's rows in ranked order: ``select_head`` columns + ``last_active`` + ``_effective_last_active``,
    exactly what ``ranked_query`` projects, read in the caller's snapshot. Rows kept by
    ``hermes_state_row_memo`` are re-projected only when their session may have changed."""
    from hermes_state_row_memo import project as kept_project
    return kept_project(conn, memo, ids_rows, prompt_join, select_head + last_active_sql, generation,
                        lambda rows: _project_fresh(conn, memo, rows, select_head, prompt_join, preview_sql,
                                                    last_active_sql, generation))


def _project_fresh(conn, memo: RankMemo, ids_rows: list, select_head: str, prompt_join: str, preview_sql: str,
                   last_active_sql: str, generation: int) -> list:
    """``project`` read from SQLite. Cached previews replace the correlated preview subquery only for rows whose
    ``transcript_revision`` still matches."""
    # The cached (revision, raw) rides on the page row itself: one PK join per row, no second json_each to
    # join against (an unindexed CTE join was quadratic on a 2000-row tree page).
    use_cache = preview_sql in select_head
    cached = {c[0]: (c[1], c[2]) for c in memo.cached_previews(generation, [r[0] for r in ids_rows])} if use_cache else {}
    page = json.dumps([[r[0], r[3], *cached.get(r[0], (None, None))] for r in ids_rows])
    head = select_head
    if use_cache:
        body = preview_sql[: -len(" AS _preview_raw")]
        head = select_head.replace(
            preview_sql, f"CASE WHEN _pg.pc_rev IS NOT NULL AND _pg.pc_rev = s.transcript_revision THEN _pg.pc_raw"
                         f" ELSE {body} END AS _preview_raw")
    sql = (f"WITH _pg(page_id, eff, pc_rev, pc_raw, ord) AS (SELECT json_extract(value, '$[0]'), "
           f"json_extract(value, '$[1]'), json_extract(value, '$[2]'), json_extract(value, '$[3]'), key "
           f"FROM json_each(?)) "
           f"{head}{last_active_sql} AS last_active, _pg.eff AS _effective_last_active "
           f"FROM _pg JOIN sessions s ON s.id = _pg.page_id {prompt_join} ORDER BY _pg.ord")
    from hermes_state_page_pack import read_page
    rows = read_page(conn, sql, (page,))
    if use_cache and rows:
        fresh = {}
        for row in rows:
            keys = row.keys()
            if "transcript_revision" in keys and cached.get(row["id"], (None,))[0] != row["transcript_revision"]:
                fresh[row["id"]] = (row["transcript_revision"], row["_preview_raw"])
        if fresh:
            memo.install_previews(generation, fresh)
    return rows


ENABLED = True  # tests and the benchmark flip it to compare against the exact ranking


def enabled() -> bool:
    return ENABLED
