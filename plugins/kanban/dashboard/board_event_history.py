"""Exact, storage-type-preserving history comparison in the caller's snapshot."""
from __future__ import annotations

import hashlib
import marshal
import sqlite3
from itertools import groupby
from operator import itemgetter
from types import MappingProxyType


def _raw(history):
    return tuple(tuple(row.values()) if isinstance(row, MappingProxyType) else tuple(row) for row in history)


def same_histories(conn, histories: dict) -> dict[str, bool]:
    """One SQL read for all ambiguous P2 invalidations; no history reductions.

    Keep the caller's snapshot: opening another connection here could compare
    against a newer commit than its task rows and event marks. Close the cursor
    before Python comparisons; the caller ends its snapshot before projection.
    """
    if not histories:
        return {}
    cur = conn.cursor()
    cur.row_factory = None
    try:
        rows = cur.execute(
            f"SELECT * FROM task_events WHERE task_id IN ({','.join('?' for _ in histories)})",
            tuple(histories)).fetchall()
        keys = tuple(d[0] for d in cur.description)
    finally:
        cur.close()
    matches = {tid: not history for tid, history in histories.items()}
    # idx_events_task is (task_id, created_at), not (task_id, id). Avoid
    # SQLite's wide-row temporary B-tree; order detached tuples in Python.
    rows.sort(key=itemgetter(keys.index('task_id'), keys.index('id')))
    for tid, group in groupby(rows, itemgetter(keys.index('task_id'))):
        history = histories[tid]
        matches[tid] = (not history or tuple(history[0].keys()) == keys) and (
            marshal.dumps(tuple(group), 2) == marshal.dumps(_raw(history), 2))
    return matches


# ``group_concat(... ORDER BY ...)`` needs SQLite 3.44; older libraries keep the full reloads.
ORDERED_AGGREGATES = sqlite3.sqlite_version_info >= (3, 44, 0)


def row_text(columns) -> str:
    """SQL text of one row, injective over SQLite storage values: storage class, then integers,
    text and blobs as hex of their bytes, reals as ``quote`` (round-trip exact). A real zero adds
    random hex: ``quote`` drops the sign of -0.0, so a zero never vouches for itself (always a miss).
    Only ``[a-z0-9A-F.:|+-]`` appear, never the ``,`` that separates rows."""
    def one(name):
        c = '"' + name.replace('"', '""') + '"'
        return (f"typeof({c})||':'||CASE typeof({c}) WHEN 'real' THEN quote({c})||"
                f"CASE WHEN {c} = 0 THEN hex(randomblob(8)) ELSE '' END ELSE hex({c}) END")
    return "||'|'||".join(one(name) for name in columns)


# The newest rows of a history are re-read on every refresh that moves it, never vouched for by
# a seal: sync stamps a fresh event after its insert (``origin_gateway`` NULL -> 'hub'), so on the
# hub the last row of a busy card has usually changed by the next read.
RECENT = 16


def _seal(columns: tuple, text: str) -> tuple:
    """``(columns, rows, length, hash, stable rows, stable length, stable hash)`` of ``text``:
    the whole history, and its head without the ``RECENT`` newest rows."""
    rows = text.count(",") + 1 if text else 0
    stable = max(rows - RECENT, 0)
    cut = len(text)
    for _ in range(RECENT if stable else 0):
        cut = text.rindex(",", 0, cut)
    h = hashlib.blake2b(text[:cut if stable else 0].encode(), digest_size=32)
    head = h.digest()
    h.update(text[cut if stable else 0:].encode())
    return columns, rows, len(text), h.digest(), stable, cut if stable else 0, head


def _ph(n: int) -> str:
    return ",".join("?" * n)


def extend(conn, wanted: dict) -> tuple[dict, dict]:
    """Retained event histories that only changed at their newest rows, without re-reading the rest.

    ``wanted`` is ``{task id: (retained EventHistory or None, its seal or None)}``. Returns
    ``(histories, seals)``: ``histories`` holds each task whose current rows, ordered by id, still
    start with the retained history's sealed head (all but its ``RECENT`` newest rows); it is that
    head plus the rows after it, read again (or the old object itself when the whole history is
    unchanged). ``seals`` holds the current seal of every wanted task, to retain with whatever the
    caller then holds for it. A seal hashes the ``row_text`` rows joined by ``,`` in id order:
    SQLite builds that text in C and returns one row per task, so a 9k-event history costs one row
    instead of 9k. Seals are only exact inside one read snapshot (the caller's transaction):
    outside one, nothing is sealed or extended.
    """
    if not wanted or not ORDERED_AGGREGATES or not conn.in_transaction:
        return {}, {}
    from hermes_cli.kanban_diagnostic_history import EventHistory

    cur = conn.cursor()
    cur.row_factory = None
    try:
        columns = tuple(d[0] for d in cur.execute("SELECT * FROM task_events LIMIT 0").description)
        ids = tuple(wanted)
        texts = dict(cur.execute(
            f"SELECT task_id, group_concat({row_text(columns)}, ',' ORDER BY id) FROM task_events "
            f"WHERE task_id IN ({_ph(len(ids))}) GROUP BY task_id", ids).fetchall())
    finally:
        cur.close()
    seals, keep, after = {}, {}, {}
    for tid, (history, seal) in wanted.items():
        text = texts.get(tid) or ""
        seals[tid] = now = _seal(columns, text)
        if not _usable(history, seal, columns, EventHistory):
            continue
        if now[1:4] == seal[1:4]:
            keep[tid] = history  # nothing changed
            continue
        stable, cut, head = seal[4:]
        if len(text) < cut or (cut and len(text) > cut and text[cut] != ",") \
                or hashlib.blake2b(text[:cut].encode(), digest_size=32).digest() != head:
            continue  # an older row changed, moved or went away: the caller reloads it
        count = text.count(",", cut) + (1 if text and not cut else 0)
        after[tid] = (history[:stable], history[stable - 1]["id"] if stable else None, count)
    keep.update(_tails(conn, after, columns, EventHistory))
    return keep, seals


def _usable(history, seal, columns, history_type) -> bool:
    return (isinstance(history, history_type) and seal is not None and seal[0] == columns
            and len(history) == seal[1] and (not history or tuple(history[0].keys()) == columns))


def _tails(conn, after: dict, columns: tuple, history_type) -> dict:
    """Each sealed head plus every row past its last id, when exactly the counted rows arrived."""
    if not after:
        return {}
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    try:
        params = [v for tid, (_h, last, _n) in after.items() for v in (tid, last)]
        rows = cur.execute(
            f"WITH w(task_id, after) AS (VALUES {','.join(['(?, ?)'] * len(after))}) "
            "SELECT e.* FROM w JOIN task_events e ON e.task_id = w.task_id "
            "AND (w.after IS NULL OR e.id > w.after) ORDER BY e.task_id, e.id", params).fetchall()
    finally:
        cur.close()
    groups = {tid: list(group) for tid, group in groupby(rows, key=lambda r: r["task_id"])}
    out = {}
    for tid, (head, _last, count) in after.items():
        group = groups.get(tid, [])
        if len(group) == count and (not group or tuple(group[0].keys()) == columns):
            out[tid] = history_type.extended(head, group)
    return out


def same_events(conn, tid, history) -> bool:
    """Scalar compatibility helper, also the pre-batch parity oracle."""
    cur = conn.cursor()
    cur.row_factory = None
    try:
        rows = cur.execute('SELECT * FROM task_events WHERE task_id = ? ORDER BY id', (tid,)).fetchall()
        keys = tuple(d[0] for d in cur.description)
        if history and tuple(history[0].keys()) != keys:
            return False
        return marshal.dumps(tuple(rows), 2) == marshal.dumps(_raw(history), 2)
    finally:
        cur.close()
