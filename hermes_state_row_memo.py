"""[daveey] Decoded list rows kept across reads, per store: an incremental projection (card t_01a21ab4eb964866).

The sidebar builds three ``list_sessions_rich(order_by_last_active=True, include_pinned=True)`` slices on each
refresh, and the default store commits every ~2 s, so request caches keyed on the file or ``data_version`` miss
on nearly every refresh. Each slice then re-ran the wide page projection (every column, the first-message preview
probe and the recency probe per row, packed and JSON-decoded) and the pinned back-fill re-read the same wide rows
of every admitted pin not on the page, although only the few sessions a write touched had changed.

This module keeps, on the store's ``RankMemo`` (``hermes_state_rank_memo``) and under its lock, each projected
row as SQLite returned it (``{session_id: {projection: row}}``), and re-reads only the rows that may differ.

A kept row equals a fresh projection of the same session in the reader's snapshot when all of these hold:

* Its session did not move on the store clock since the row was installed. ``RankMemo.advance`` drops a
  session's rows when its ``sessions`` row changed in any column outside ``COUNTER_COLUMNS``
  (``hermes_state_session_change``: insert, delete, re-key and every watched column, pin/unpin and archive
  included) or a message was appended to it (``messages.id`` watermark), and drops every row when the clock is
  untrusted (cursor FULL/None: file replaced, schema or trigger change, restore) or the lowered counter moved (a
  message deleted, (de)activated, or its ``timestamp``/``session_id`` rewritten). The projection reads nothing
  else: the compact ``sessions`` columns, its own messages for ``_preview_raw`` and ``last_active``.
* Its counter columns (``COUNTER_COLUMNS``, which the cursor ignores) are equal, value and SQLite type, in this
  snapshot: one narrow PK read checks them (``_stale``). ``transcript_revision`` is one of them, and the
  ``messages_*_revision_update`` triggers bump it on any role, content or display_kind rewrite (the preview's
  inputs); ``RankMemo.preview_ok`` proves those triggers are exactly installed, else nothing is kept.
* Lookups and installs happen only at the generation this reader's own snapshot advanced the clock to: a row
  installed by a newer snapshot is never served to an older one, and an older reader never installs.

Only the compact projection (no ``system_prompts`` join) is kept. ``_effective_last_active`` is the ranking's
value, never kept: it is re-attached from the caller's ranked ids. Rows are handed out as shallow copies (every
value is a scalar). Nothing expires on a clock: no TTL.
"""

from __future__ import annotations

import json
from collections import Counter, OrderedDict
from typing import Any, Callable

ENABLED = True  # [daveey] tests flip it to compare against the full projection
STATS: Counter = Counter()  # kept / projected rows (tests, benchmarks)
_MAX_SESSIONS = 4096
_MAX_PREVIEW_CHARS = 4096
_EFFECTIVE = "_effective_last_active"


def _counter_columns(row: dict) -> list:
    from hermes_state_session_change import COUNTER_COLUMNS
    return [name for name in row if name in COUNTER_COLUMNS]


def _keepable(row: dict) -> bool:
    raw = row.get("_preview_raw")
    return (isinstance(row.get("id"), str) and "transcript_revision" in row
            and (raw is None or (isinstance(raw, str) and len(raw) <= _MAX_PREVIEW_CHARS)))


class RowMemo:
    """``RankMemo.rows``. Every method runs under ``RankMemo.lock`` (``project`` takes it)."""

    def __init__(self) -> None:
        self.sessions: OrderedDict[str, dict] = OrderedDict()

    def clear(self) -> None:
        self.sessions.clear()

    def drop(self, session_id) -> None:
        self.sessions.pop(session_id, None)

    def get(self, head: str, ids: list) -> dict:
        out = {}
        for sid in ids:
            kept = self.sessions.get(sid)
            row = kept.get(head) if kept is not None else None
            if row is not None:
                self.sessions.move_to_end(sid)
                out[sid] = row
        return out

    def put(self, head: str, rows: list) -> None:
        for row in rows:
            if not _keepable(row):
                continue
            sid = row["id"]
            kept = self.sessions.get(sid)
            if kept is None:
                kept = self.sessions[sid] = {}
            kept[head] = {k: v for k, v in row.items() if k != _EFFECTIVE}
            self.sessions.move_to_end(sid)
        while len(self.sessions) > _MAX_SESSIONS:
            self.sessions.popitem(last=False)


def _usable(memo, generation: int) -> bool:
    return ENABLED and memo.clock is not None and memo.preview_ok and generation == memo.generation


def _stale(conn, hits: dict) -> set:
    """Ids among ``hits`` whose counter columns differ (value or SQLite type) in this snapshot, or that are gone."""
    from hermes_state_page_pack import read_rows
    cols = _counter_columns(next(iter(hits.values())))
    names = ", ".join('s."' + c.replace('"', '""') + '"' for c in cols)
    sql = f"SELECT s.id, {names} FROM json_each(?) j JOIN sessions s ON s.id = j.value"
    fresh = {r[0]: r[1:] for r in read_rows(conn, sql, (json.dumps(sorted(hits)),), 1 + len(cols))}
    stale = set()
    for sid, row in hits.items():
        now = fresh.get(sid)
        if now is None or [(type(v), v) for v in now] != [(type(row[c]), row[c]) for c in cols]:
            stale.add(sid)
    return stale


def project(conn, memo, ids_rows: list, prompt_join: str, head: str, generation: int,
            fetch: Callable[[list], list]) -> list:
    """``fetch(ids_rows)``'s rows for ``ids_rows`` (ranked order), re-projecting only rows not provably kept."""
    if prompt_join or not ids_rows:
        return fetch(ids_rows)
    ids = [r[0] for r in ids_rows]
    with memo.lock:
        hits = memo.rows.get(head, ids) if _usable(memo, generation) else {}
    if hits:
        for sid in _stale(conn, hits):
            del hits[sid]
    missing = [r for r in ids_rows if r[0] not in hits]
    STATS["kept"] += len(hits)
    STATS["projected"] += len(missing)
    fetched = {row["id"]: row for row in fetch(missing)} if missing else {}
    if fetched:
        with memo.lock:
            if _usable(memo, generation):
                memo.rows.put(head, list(fetched.values()))
    out = []
    for r in ids_rows:
        row = fetched.get(r[0])
        if row is None:
            kept: Any = hits.get(r[0])
            if kept is None:
                continue  # the fresh projection dropped it too (gone in this snapshot)
            row = dict(kept)
            row[_EFFECTIVE] = r[3]
        out.append(row)
    return out
