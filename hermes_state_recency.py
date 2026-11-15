"""``list_sessions_rich(order_by_last_active=True)`` pages without a messages probe per session.

The exact ranking (``ranked_query``) orders every admitted row by its chain's freshest
``_sql_session_last_active``, whose ``MAX(messages.timestamp)`` probe costs one index seek per
session: ~100 ms of CPU per read on a 22k-session store, and a sidebar poll misses its caches about
once a second because some session row changes that often. ``ranked_rows`` returns the same rows
while running that probe only on a small candidate set that provably contains the page:

* A per-store cache keeps each session's in-window ``MAX(messages.timestamp)``, built once with
  the same probe. Every read folds in the sessions of messages past the watermark, inside the
  read's own snapshot (``messages.id`` is AUTOINCREMENT, so a new message always lands past it).
  A delete or rewind can only lower the true max, so a cached value is an UPPER bound.
* A row's exact recency is the MAX of its in-window ``last_activity_at`` and message max, else its
  in-window ``started_at``; a chain root's is the MAX over its compression chain. So
  ``max(last_activity_at, cached max, started_at)`` of some chain member bounds it from above, and
  the row's own in-window ``last_activity_at`` bounds it from below.
* ``T`` = the (offset+limit)-th largest in-window ``last_activity_at`` among admitted rows (read
  off ``idx_sessions_effective_activity``). That many rows rank at ``>= T``, so every row of the
  page ranks ``>= T`` and has a chain member whose upper bound is ``>= T``. The candidates are every
  session with ``last_activity_at``/``started_at`` ``>= T`` (index ranges) or a cached max ``>= T``,
  plus all their ancestors. A row outside them ranks strictly below ``T``: it cannot reach or tie
  into the page, and the exact query restricted to the candidates returns exactly the unrestricted
  query's page, in SQLite's own ordering.

Fewer than offset+limit in-window ``last_activity_at`` rows, a search, a store without
AUTOINCREMENT ids, a watermark that moved backwards (another file) or any SQLite error falls back
to the unrestricted query. Assumption: a message's ``timestamp`` and ``session_id`` are not raised or
moved in place and no message is inserted with an explicit old id on a live store (no writer does;
the salvage repair rewrites timestamps only on a new file, which starts a new cache).
"""
import json
import os
import sqlite3
import threading
from collections import OrderedDict
from typing import Any, Optional

from hermes_state_common import _SQL_IN_WINDOW, _sql_in_window, _sql_json_extract, _RESET_CHILD_SQL
from hermes_state_common import _sql_session_last_active, _sql_session_last_active_by_id
from hermes_cli.timefmt import EPOCH_MAX

ENABLED = True  # tests flip it to compare against the unrestricted query
# [daveey] D2: every profile's store must stay resident (the hub serves 11 profiles; an LRU of 8 evicted
# and rebuilt a store on each fan-out). The routes reserve ``profiles + 4`` (reserve_stores).
_MAX_STORES = 8
_reserved_stores = 0
_STORES: "OrderedDict[tuple, _StoreRecency]" = OrderedDict()
_STORES_LOCK = threading.Lock()


def reserve_stores(count: int) -> None:
    """Keep at least ``count`` stores resident for the rest of this process (never shrinks)."""
    global _reserved_stores
    with _STORES_LOCK:
        _reserved_stores = max(_reserved_stores, int(count))


def max_stores() -> int:
    return max(_MAX_STORES, _reserved_stores)


def _msg_max_sql(session_id_expr: str) -> str:
    # The same probe _sql_freshest_of embeds, so a cached value is the value the exact query reads.
    return (f"(SELECT MAX(_rc_m.timestamp) FROM messages _rc_m INDEXED BY idx_messages_session"
            f" WHERE _rc_m.session_id = {session_id_expr} AND _rc_m.timestamp {_SQL_IN_WINDOW})")


def chain_cte(seed_from: str, where_sql: str) -> str:
    return f"""chain(root_id, cur_id) AS (
                    SELECT s.id, s.id FROM {seed_from} {where_sql}
                    UNION ALL
                    SELECT c.root_id, child.id
                    FROM chain c
                    JOIN sessions parent ON parent.id = c.cur_id
                    JOIN sessions child ON child.parent_session_id = c.cur_id
                    WHERE parent.end_reason = 'compression'
                      AND {_sql_json_extract('child.model_config', '$._branched_from')} IS NULL
                      AND {_sql_json_extract('child.model_config', '$._delegate_from')} IS NULL
                      AND NOT ({_RESET_CHILD_SQL.format(a='child')})
                      AND COALESCE(child.source, '') != 'tool'
                )"""


def ranked_query(where_sql: str, outer_where: str, seed_from: str, select_head: str, prompt_join: str) -> str:
    """The exact chain-tip ranking: the CTE walks compression-continuation edges forward from the
    admitted rows; MAX over the chain gives effective_last_active in SQL. Do NOT require
    child.started_at >= parent.ended_at: races insert the continuation before ended_at is written.

    The page is ranked and cut on ids alone (``page``); only its rows then pay for the projection.
    Projecting in the ranked query made SQLite build every admitted row's full result (s.*, the
    resolved system prompt, the preview subquery over message content) before LIMIT: 4-11s per
    page at 25k rows, for LIMIT 1 as much as LIMIT 200. Binds: where (seed), outer, LIMIT, OFFSET."""
    return f"""
                WITH RECURSIVE {chain_cte(seed_from, where_sql)},
                chain_max AS (
                    SELECT
                        root_id,
                        MAX({_sql_session_last_active_by_id("cur_id")}) AS effective_last_active
                    FROM chain
                    GROUP BY root_id
                ),
                page AS (
                    SELECT s.id AS page_id,
                        COALESCE(cm.effective_last_active, {_sql_session_last_active("s")},
                                 {_sql_in_window("s.started_at")}) AS effective_last_active
                    FROM sessions s
                    LEFT JOIN chain_max cm ON cm.root_id = s.id
                    {outer_where}
                    ORDER BY effective_last_active DESC, s.started_at DESC, s.id DESC
                    LIMIT ? OFFSET ?
                )
                {select_head}{_sql_session_last_active("s")} AS last_active,
                    page.effective_last_active AS _effective_last_active
                FROM page
                JOIN sessions s ON s.id = page.page_id
                {prompt_join}
                ORDER BY _effective_last_active DESC, s.started_at DESC, s.id DESC
            """


# Every session with a bound >= ? (three binds of the threshold, then the JSON list of sessions whose
# cached message max reaches it), plus all their ancestors: a chain root ranks by its members.
# UNION ALL keeps each arm on its own index (a UNION merge scanned the PK instead); ``up`` dedupes.
_CANDIDATES_SQL = f"""
    WITH RECURSIVE
    seed(id) AS (
        SELECT id FROM sessions WHERE COALESCE(last_activity_at, started_at) >= ?
        UNION ALL SELECT id FROM sessions WHERE started_at BETWEEN ? AND {EPOCH_MAX!r}
        UNION ALL SELECT value FROM json_each(?)
    ),
    up(id) AS (
        SELECT id FROM seed
        UNION SELECT s.parent_session_id FROM up JOIN sessions s ON s.id = up.id
        WHERE s.parent_session_id IS NOT NULL
    )
    SELECT id FROM up"""


class _StoreRecency:
    """One store's ``{session_id: cached in-window MAX(messages.timestamp)}`` and its watermark."""

    def __init__(self):
        self.lock = threading.Lock()
        self.watermark: Optional[int] = None  # None: never built
        self.msg_max: dict[str, Any] = {}
        self.stale = False
        from hermes_state_rank_memo import RankMemo
        self.rank_memo = RankMemo(self.lock)  # [daveey] D2: ranked page ids + previews (same lock)

    def probe(self, conn, ids) -> None:
        ids = list(ids)
        if not ids:
            return
        for sid, value in conn.execute(f"SELECT j.value, {_msg_max_sql('j.value')} FROM json_each(?) j",
                                       (json.dumps(ids),)).fetchall():
            if value is None:
                self.msg_max.pop(sid, None)
            else:
                self.msg_max[sid] = value

    def refresh(self, conn) -> bool:
        """Fold this snapshot's new messages in; False when ids cannot carry a watermark."""
        seq_row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'messages'").fetchone()
        max_id = conn.execute("SELECT MAX(id) FROM messages").fetchone()[0]
        if seq_row is None and max_id is not None:
            return False  # ids may be reused (no AUTOINCREMENT): a watermark proves nothing
        high = max(int(seq_row[0]) if seq_row else 0, int(max_id or 0))
        if self.watermark is None or self.stale or high < self.watermark:
            self.msg_max, self.stale = {}, False
            for sid, value in conn.execute(f"SELECT s.id, {_msg_max_sql('s.id')} FROM sessions s").fetchall():
                if value is not None:
                    self.msg_max[sid] = value
        elif high > self.watermark:
            # NOT INDEXED: walk the rowid range, never the whole (session_id, timestamp) index.
            self.probe(conn, {sid for (sid,) in conn.execute(
                "SELECT session_id FROM messages NOT INDEXED WHERE id > ?", (self.watermark,)).fetchall()})
        self.watermark = high
        return True


def _store_for(path: str) -> Optional[_StoreRecency]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (os.path.realpath(path), st.st_dev, st.st_ino)  # a replaced file starts over
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = _STORES[key] = _StoreRecency()
        _STORES.move_to_end(key)
        while len(_STORES) > max(_MAX_STORES, _reserved_stores):
            _STORES.popitem(last=False)
        return store


def clear() -> None:
    with _STORES_LOCK:
        _STORES.clear()


def _candidates(conn, store, where_clauses, params, need) -> Optional[list[str]]:
    """Session ids that contain the page (module doc), or None to read unrestricted."""
    if not store.refresh(conn):
        return None
    # Among rows with an in-window last_activity_at the index key IS last_activity_at. CASE keeps the
    # admission filter (JSON markers) off every row that has none.
    in_window = f"s.last_activity_at {_SQL_IN_WINDOW}"
    admitted = " AND ".join(f"({clause})" for clause in where_clauses) or "1"
    row = conn.execute(
        f"SELECT s.last_activity_at FROM sessions s WHERE CASE WHEN {in_window} THEN {admitted} ELSE 0 END"
        " ORDER BY COALESCE(s.last_activity_at, s.started_at) DESC LIMIT 1 OFFSET ?",
        [*params, need - 1]).fetchone()
    if row is None:
        return None
    threshold = row[0]
    high = [sid for sid, value in store.msg_max.items() if value >= threshold]
    if len(high) > 2 * need + 64:
        # Cached maxima a delete lowered stay high until probed again: re-probe them once.
        store.probe(conn, high)
        high = [sid for sid in high if store.msg_max.get(sid, threshold - 1) >= threshold]
    return [sid for (sid,) in conn.execute(
        _CANDIDATES_SQL, (threshold, threshold, json.dumps(high))).fetchall()]


def ranked_rows(db, where_clauses: list[str], params: list, select_head: str, prompt_join: str,
                id_needle: str, search_needle: str, limit: int, offset: int) -> list:
    """The ``order_by_last_active`` page of list_sessions_rich (see module doc).

    Unless a search needs every row's chain (it matches a row's own id and title through it), only
    rows that HAVE a child seed the walk: a childless row's chain is itself alone, so its MAX is just
    its own last_active, which the outer COALESCE reads directly. Drive from distinct parent ids via
    idx_sessions_parent, then look up each seed by PK (a correlated EXISTS still parsed every
    childless row's JSON before rejecting it)."""
    from hermes_state_sessions import _where_sql
    where_sql = _where_sql(where_clauses)
    seed_from = "sessions s" if (id_needle or search_needle) else (
        "(SELECT DISTINCT parent_session_id FROM sessions WHERE parent_session_id IS NOT NULL) roots "
        "CROSS JOIN sessions s ON s.id = roots.parent_session_id")
    outer_where, id_params = db._chain_search_where(where_sql, id_needle, search_needle)
    query = ranked_query(where_sql, outer_where, seed_from, select_head, prompt_join)
    query_params = params + params + id_params + [limit, offset]
    need = (offset or 0) + (limit or 0)
    path = str(getattr(db, "db_path", "") or "")
    store = _store_for(path) if ENABLED and path and not (id_needle or search_needle) else None
    if store is None or limit is None or limit <= 0 or (offset or 0) < 0:
        return db._read_all(query, query_params)
    import hermes_state_rank_memo as rank_memo
    if not offset and rank_memo.enabled():
        try:
            return _memo_rows(db, store, rank_memo, where_sql, where_clauses, params, outer_where, id_params,
                              seed_from, select_head, prompt_join, need)
        except sqlite3.Error:
            with store.lock:
                store.stale = True
                store.rank_memo._reset()
            return db._read_all(query, query_params)
    try:
        with db._read_ctx() as conn:
            began = not conn.in_transaction
            if began:
                conn.execute("BEGIN")  # one snapshot for the bounds and the restricted exact query
            try:
                with store.lock:
                    picked = _candidates(conn, store, where_clauses, params, need)
                if picked is None:
                    return conn.execute(query, query_params).fetchall()
                narrowed = _where_sql([*where_clauses, "s.id IN (SELECT value FROM json_each(?))"])
                narrowed_params = [*params, json.dumps(picked)]
                return conn.execute(ranked_query(narrowed, narrowed, seed_from, select_head, prompt_join),
                                    narrowed_params + narrowed_params + [limit, offset]).fetchall()
            finally:
                if began and conn.in_transaction:
                    conn.execute("ROLLBACK")
    except sqlite3.Error:
        with store.lock:
            store.stale = True
        return db._read_all(query, query_params)  # reports a real storage fault through its own path


def _memo_rows(db, store, rank_memo, where_sql, where_clauses, params, outer_where, id_params, seed_from,
               select_head, prompt_join, need) -> list:
    """[daveey] D2: the first page from the store's ranked-id memo (hermes_state_rank_memo), projected in
    the same snapshot. Same rows and columns as the exact query."""
    from hermes_state_common import _sql_preview_raw
    from hermes_state_sessions import _where_sql
    ids_head = rank_memo._IDS_HEAD
    key = (where_sql, repr(tuple(params)), seed_from, need)

    def restricted(ids, limit):
        narrowed = _where_sql([*where_clauses, "s.id IN (SELECT value FROM json_each(?))"])
        narrowed_params = [*params, json.dumps(ids)]
        return conn.execute(ranked_query(narrowed, narrowed, seed_from, ids_head, ""),
                            narrowed_params + narrowed_params + [limit, 0]).fetchall()

    def exact_ids(*, invalidate_candidates=False):
        if invalidate_candidates:
            # An in-place message edit can raise a row outside D1's cached candidates.
            store.stale = True
        picked = _candidates(conn, store, where_clauses, params, need)
        if picked is None:
            return conn.execute(ranked_query(where_sql, outer_where, seed_from, ids_head, ""),
                                params + params + id_params + [need, 0]).fetchall()
        return restricted(picked, need)

    with db._read_ctx() as conn:
        began = not conn.in_transaction
        if began:
            conn.execute("BEGIN")  # one snapshot: the clock, the ranking and the projection
        try:
            with store.lock:
                ids_rows = rank_memo.ranked_ids(conn, store.rank_memo, key, need, exact_ids,
                                                lambda ids: restricted(ids, -1))
                generation = store.rank_memo.generation
            if not ids_rows:
                return []
            return rank_memo.project(conn, store.rank_memo, ids_rows, select_head, prompt_join, _sql_preview_raw(),
                                     _sql_session_last_active("s"), generation)
        finally:
            if began and conn.in_transaction:
                conn.execute("ROLLBACK")
