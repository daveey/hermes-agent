"""Done cards a session created or shepherds, completed since a cutoff (the attention pass's board read).

Split from ``kanban_session_activity`` (its file cap); ``_read_recent_done`` there delegates here. [daveey]
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

from hermes_cli import kanban_lockguard as _kanban_lockguard


MAX_DONE_PAIRS = 200  # newest completions per board and statement; older ones are simply not observed
_DONE_COND = "t.status = 'done' AND t.completed_at >= ?"


def done_pairs_on(conn: sqlite3.Connection, since: float, counted_sub_clause: Callable[[sqlite3.Connection], str],
                  limit: int = MAX_DONE_PAIRS) -> list[tuple[str, str]]:
    """``(session_id, task_id)`` of the same done cards ``read_recent_done`` lists, on the caller's transaction.

    Two bounded statements per board, whatever the card count: one for created links, one for tui
    subscriptions. Newest first, capped at ``limit`` each.
    """
    pairs = [(str(r[0]), str(r[1])) for r in conn.execute(
        "SELECT t.session_id, t.id FROM tasks t WHERE t.session_id IS NOT NULL AND t.session_id != '' "
        f"AND {_DONE_COND} ORDER BY t.completed_at DESC, t.id LIMIT ?", (since, limit))]
    try:
        pairs.extend((str(r[0]), str(r[1])) for r in conn.execute(
            "SELECT s.chat_id, t.id FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
            f"WHERE s.platform = 'tui' AND s.chat_id != '' AND {counted_sub_clause(conn)} AND {_DONE_COND} "
            "ORDER BY t.completed_at DESC, t.id LIMIT ?", (since, limit)))
    except sqlite3.Error:  # board predates notify subscriptions
        pass
    return list(dict.fromkeys(pairs))


def read_recent_done(path: Path, since: float, counted_sub_clause: Callable[[sqlite3.Connection], str],
                     board_slug: Callable[[Path], str]) -> list[tuple[str, dict[str, Any]]]:
    """``(session_id, card)`` for done cards completed since ``since`` that a session created or shepherds."""
    conn = _kanban_lockguard.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    conn.row_factory = sqlite3.Row
    try:
        has_runs = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_runs'").fetchone() is not None
        summary = ("COALESCE((SELECT r.summary FROM task_runs r WHERE r.task_id = t.id AND r.summary IS NOT NULL "
                   "AND r.summary != '' ORDER BY r.id DESC LIMIT 1), t.result)") if has_runs else "t.result"
        fields = f"t.id AS id, t.title AS title, t.completed_at AS completed_at, {summary} AS summary"
        cond = _DONE_COND
        pairs = [(str(r["session_id"]), dict(r)) for r in conn.execute(
            f"SELECT t.session_id AS session_id, {fields} FROM tasks t WHERE t.session_id IS NOT NULL "
            f"AND t.session_id != '' AND {cond}", (since,))]
        try:
            pairs.extend((str(r["chat_id"]), dict(r)) for r in conn.execute(
                f"SELECT s.chat_id AS chat_id, {fields} FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
                f"WHERE s.platform = 'tui' AND s.chat_id != '' AND {counted_sub_clause(conn)} AND {cond}",
                (since,)))
        except sqlite3.Error:  # board predates notify subscriptions
            pass
    finally:
        conn.close()
    board = board_slug(path)
    out = []
    for sid, card in pairs:
        card.pop("session_id", None)
        card.pop("chat_id", None)
        out.append((sid, {**card, "board": board, "board_path": str(path)}))
    return out


# [daveey] Per (origin scope, board): (memo key, since, rows) of its last read. The attention pass re-read every
# board whenever any board's revision moved; a board whose key (``kanban_session_activity_revalidate.board_memo_key``:
# content revision, or stat plus the reader's data_version) is unchanged holds the same done cards, and a later
# cut-off only drops rows. Unknown or busy keys equal nothing, so they always read (t_01a1cee98aaa8db1).
_memo: dict[tuple, tuple[Any, float, list[tuple[str, dict[str, Any]]]]] = {}
_memo_lock = threading.Lock()
MEMO_MAX = 256


def memoized(path: Path, since: float, read: Callable[[Path, float], list[tuple[str, dict[str, Any]]]],
             memo_key: Callable[[Path], Any], scope: tuple = ()) -> list[tuple[str, dict[str, Any]]]:
    """``read(path, since)``, served from this scope's last read of the board while its ``memo_key`` is unchanged and
    ``since`` is no earlier than that read's. Returns fresh card dicts (callers and checks may hold them)."""
    slot = (scope, str(path))
    try:
        key = memo_key(path)  # before the read: a commit during it moves the key for the next call
    except sqlite3.Error:
        # No key vouches for a memo row (unopenable/vanished board): read as before, never cache, and let the
        # read decide whether this board counts.
        with _memo_lock:
            _memo.pop(slot, None)
        return read(path, since)
    with _memo_lock:
        hit = _memo.get(slot)
    if hit is not None and hit[0] == key and since >= hit[1]:
        kept: list[tuple[str, dict[str, Any]]] = []
        for sid, card in hit[2]:
            at = card.get("completed_at")
            if not isinstance(at, (int, float)) or isinstance(at, bool):
                break  # a row without a comparable cut-off: read again
            if at >= since:
                kept.append((sid, dict(card)))
        else:
            return kept
    rows = read(path, since)
    with _memo_lock:
        _memo.pop(slot, None)
        _memo[slot] = (key, since, [(sid, dict(card)) for sid, card in rows])
        while len(_memo) > MEMO_MAX:
            del _memo[next(iter(_memo))]
    return rows


def reset() -> None:
    with _memo_lock:
        _memo.clear()
