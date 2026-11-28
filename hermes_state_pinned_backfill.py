"""Pinned back-fill of ``list_sessions_rich(include_pinned=True)``: hydrate only pins the page missed. [daveey]

The back-fill used to read and decode the full row of EVERY admitted pin (wide row, correlated preview and
recency probes, packed transport prepared twice), and Python then dropped those already on the page. The
sidebar's default store has many sessions but few pins (card t_01a21ab4eb964866), and live writes invalidate
its request cache frequently.

Now SQL drops the page's ids (``s.id NOT IN``), and a narrow probe without the projection first asks whether
any pin is left at all; only then does the wide read run. Probe and read share ONE read snapshot, and the read
is the old query with one more conjunct (same driving index, filters and ``started_at DESC`` sort), so it
returns exactly the old rows minus the page's ids, in the old order. Ids are unique (PK; the prompt join is on
a PK), so the old Python dedupe had nothing else to drop. No TTL: when the store clock can vouch for it, the
read takes only the ordered ids and projects just the pins whose rows may have changed (``_kept_rows``,
``hermes_state_row_memo``).
"""
import json

from hermes_state_common import _sql_session_last_active

ENABLED = True  # [daveey] Tests compare against the full back-fill, filtered in Python.

# [daveey] Same FROM as the wide read; the optional prompt join is LEFT and cannot exclude a pin.
_PIN_FROM = """FROM (SELECT id AS _pin_id FROM sessions WHERE pinned = 1) pin
                CROSS JOIN sessions s ON s.id = pin._pin_id"""


def _pinned_query(select_head: str, prompt_join: str, where_sql: str) -> str:
    # [daveey] Driven from the (few) pinned ids rather than a started_at inventory walk.
    return f"""
                {select_head}{_sql_session_last_active("s")} AS last_active
                {_PIN_FROM}
                {prompt_join}
                {where_sql}
                ORDER BY s.started_at DESC
            """


def backfill_pins(db, sessions: list, select_head: str, prompt_join: str, clauses: list, params: list) -> None:
    """Append (in place) the admitted pins of ``clauses`` that ``sessions`` lacks, ``started_at DESC``."""
    from hermes_state_page_pack import read_chunks
    from hermes_state_sessions import _where_sql
    seen_ids = {s["id"] for s in sessions}
    # [daveey] Legacy/dirty BLOB or NULL identities cannot travel through a JSON string set.
    if not ENABLED or any(not isinstance(sid, str) for sid in seen_ids):
        query = _pinned_query(select_head, prompt_join, _where_sql(clauses))
        for row in db._read_retrying_ioerr(lambda conn: read_chunks(conn, query, params)):
            s = db._list_row(row)
            if s["id"] not in seen_ids:
                seen_ids.add(s["id"])
                sessions.append(s)
        return
    where_sql = _where_sql([*clauses, "s.id NOT IN (SELECT value FROM json_each(?))"])
    params = [*params, json.dumps(sorted(seen_ids))]

    def read(conn):
        began = not conn.in_transaction
        if began:
            conn.execute("BEGIN")  # [daveey] Keep the probe and hydration on one snapshot.
        try:
            if conn.execute(f"SELECT 1 {_PIN_FROM} {where_sql} LIMIT 1", params).fetchone() is None:
                return []
            kept = _kept_rows(db, conn, select_head, prompt_join, where_sql, params)
            if kept is not None:
                return kept
            return read_chunks(conn, _pinned_query(select_head, prompt_join, where_sql), params)
        finally:
            if began and conn.in_transaction:
                conn.execute("ROLLBACK")

    sessions.extend(db._list_row(row) for row in db._read_retrying_ioerr(read))


def _kept_rows(db, conn, select_head: str, prompt_join: str, where_sql: str, params: list):
    """[daveey] The back-fill's rows through the store's kept rows (``hermes_state_row_memo``), or None for the
    wide read. Same snapshot, same FROM/WHERE/ORDER BY: only the ordered ids are read here, then the store clock
    is advanced to this snapshot and only rows not provably unchanged are projected. A ``started_at`` tie (the
    wide read's order between tied rows is the sorter's, not the ids') takes the wide read."""
    import hermes_state_rank_memo as rank_memo
    import hermes_state_recency as recency
    import hermes_state_row_memo as row_memo
    from hermes_state_common import _sql_preview_raw
    path = str(getattr(db, "db_path", "") or "")
    preview_sql = _sql_preview_raw()
    # Only a projection whose rows can be kept (the compact list head: preview and transcript_revision).
    if (prompt_join or not path or preview_sql not in select_head or "s.transcript_revision" not in select_head
            or not (row_memo.ENABLED and recency.ENABLED and rank_memo.enabled())):
        return None
    from hermes_state_page_pack import read_chunks
    # The wide read's pin-driven plan and chunked transport, narrowed to the ordered ids (types preserved).
    pins = [(r["id"], r["started_at"]) for r in read_chunks(
        conn, f"SELECT s.id AS id, s.started_at AS started_at {_PIN_FROM} {where_sql} ORDER BY s.started_at DESC",
        params)]
    if not pins:
        return []
    if any(not isinstance(sid, str) for sid, _ in pins) or len({rank_memo._v(at) for _, at in pins}) < len(pins):
        return None
    store = recency._store_for(path)
    if store is None:
        return None
    with store.lock:
        reason = store.rank_memo.advance(conn)
        if reason is not None:
            store.stale = True  # what ranked_ids' exact path does for its own advance (D1 candidates)
        generation = store.rank_memo.generation
        if store.rank_memo.clock is None:
            return None  # the store cannot vouch for changes right now: nothing is kept, read wide
    return rank_memo.project(conn, store.rank_memo, [(sid, None, None, None) for sid, _ in pins], select_head,
                             prompt_join, preview_sql, _sql_session_last_active("s"), generation)
