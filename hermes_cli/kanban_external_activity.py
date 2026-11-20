"""External-run cards on the read side: wait info and the session-row counts/cards. [daveey]

``overlay_waits`` adds ``wait_state`` (external_running / external_unknown / external_down) and the ``external``
dict to a parked card's wait info (interface.md); ``session_external`` stamps the three counts and the capped
``kanban_external_cards`` list on session rows (interface-hover.md). Counts are subsets of ``kanban_parked`` and
are never added to ``kanban_blocked``. Local boards only: a card replicated from another gateway keeps today's
grey wait info.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

CARDS_CAP = 10
_TTL_S = 5.0
_lock = threading.Lock()
_memo: dict[str, tuple[float, dict]] = {}


def overlay_waits(conn: sqlite3.Connection, info: dict[str, dict[str, Any]], now: Optional[float] = None) -> None:
    """Add the external state to ``info`` (``{task_id: wait info}``) in place. Never raises."""
    if not info:
        return
    try:
        from hermes_cli import kanban_external_watch as kew
        for tid, derived in kew.card_states(conn, list(info), now=now).items():
            info[tid]["wait_state"] = derived["state"]
            info[tid]["external"] = derived["external"]
    except (sqlite3.Error, ImportError, KeyError, TypeError, ValueError):
        return


def session_external(conn: sqlite3.Connection, pairs: Iterable[tuple[str, str]], key: str = "",
                     now: Optional[float] = None) -> dict[str, dict[str, Any]]:
    """``{session id: {"counts": (external, unknown, down), "cards": [{task_id, title, external, state}]}}`` for
    the parked (session, task) ``pairs`` of one board, memoised ``_TTL_S`` per ``key``. Cards newest-park first."""
    wall = time.time() if now is None else now
    with _lock:
        hit = _memo.get(key)
        if key and hit and wall - hit[0] < _TTL_S:
            return hit[1]
    pairs = list(pairs)
    out: dict[str, dict[str, Any]] = {}
    try:
        from hermes_cli import kanban_external_watch as kew
        states = kew.card_states(conn, {t for _s, t in pairs}, now=wall)
        titles = {}
        ids = list(states)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            titles.update((str(t), str(n or "")) for t, n in conn.execute(
                f"SELECT id, title FROM tasks WHERE id IN ({','.join('?' for _ in chunk)})", chunk))
        for sid, tid in sorted(pairs):
            derived = states.get(tid)
            if derived is None:
                continue
            slot = out.setdefault(sid, {"counts": [0, 0, 0], "cards": []})
            if derived["state"] == "external_down":
                slot["counts"][2] += 1
            else:
                slot["counts"][0] += 1
                slot["counts"][1] += derived["state"] == "external_unknown"
            ext = dict(derived["external"], state=derived["state"])
            slot["cards"].append({"task_id": tid, "title": titles.get(tid, ""), "external": ext})
        for slot in out.values():
            slot["cards"].sort(key=lambda c: (c["external"].get("observed_at") or 0, c["task_id"]), reverse=True)
    except (sqlite3.Error, ImportError, KeyError, TypeError, ValueError):
        out = {}
    with _lock:
        if key:
            _memo[key] = (wall, out)
    return out


def stamp_rows(rows: list[dict[str, Any]], by_session: dict[str, dict[str, Any]], ids_of) -> None:
    """Stamp ``kanban_external``, ``kanban_external_unknown``, ``kanban_external_down`` and
    ``kanban_external_cards`` (cap ``CARDS_CAP``) on each row from the per-session map; absent when zero."""
    for row in rows:
        counts, cards, seen = [0, 0, 0], [], set()
        for sid in ids_of(row):
            slot = by_session.get(sid)
            if not slot:
                continue
            counts = [a + b for a, b in zip(counts, slot["counts"])]
            cards += [c for c in slot["cards"] if c["task_id"] not in seen and not seen.add(c["task_id"])]
        for field, n in (("kanban_external", counts[0]), ("kanban_external_unknown", counts[1]),
                         ("kanban_external_down", counts[2])):
            if n:
                row[field] = n
            else:
                row.pop(field, None)
        if cards:
            cards.sort(key=lambda c: (c["external"].get("observed_at") or 0, c["task_id"]), reverse=True)
            row["kanban_external_cards"] = cards[:CARDS_CAP]
        else:
            row.pop("kanban_external_cards", None)


def stamp_boards(rows: list[dict[str, Any]], board_paths, open_ro, parked_pairs, row_ids) -> list[dict[str, Any]]:
    """Read every local board's parked (session, task) pairs and stamp ``rows``; returns ``rows``. Never raises:
    the session list must render without the external fields."""
    by_session: dict[str, dict[str, Any]] = {}
    try:
        for path in board_paths():
            try:
                conn = open_ro(path)
            except sqlite3.Error:
                continue
            try:
                pairs = parked_pairs(conn)
                for sid, slot in (session_external(conn, pairs, key=str(path)) if pairs else {}).items():
                    mine = by_session.setdefault(sid, {"counts": [0, 0, 0], "cards": []})
                    mine["counts"] = [a + b for a, b in zip(mine["counts"], slot["counts"])]
                    mine["cards"] += slot["cards"]
            except sqlite3.Error:
                logger.debug("kanban external read failed for %s", path, exc_info=True)
            finally:
                conn.close()
        stamp_rows(rows, by_session, row_ids)
    except (sqlite3.Error, OSError, ImportError, KeyError, TypeError, ValueError, AttributeError):
        logger.debug("kanban external stamping failed", exc_info=True)
    return rows


def reset_for_tests() -> None:
    with _lock:
        _memo.clear()
