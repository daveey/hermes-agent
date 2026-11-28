"""SessionState v2 input collection: every read the pure reducer needs, for one lineage. [daveey]

The collector is the only I/O in a recompute. It reuses the v1 canonical readers
(``hermes_state_session_status_snapshot.collect_parts`` with raw card rows, so clock-derived card state is decided
by the reducer at its own ``now``) and adds what v2 shows beyond v1: each linked local card's dependency closure
(``task_links`` parents, bounded), and the current block episode of every blocked/triage card (the lifecycle event
that started it, so a resolution binds to one episode and a later re-block is red again).

A reader that raises is recorded under ``errors`` (unknown coverage), never as an empty reading.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

MAX_CLOSURE = 200  # dependency-closure cards per lineage
MAX_DEPTH = 8
_ROW_FIELDS = ("id", "title", "status", "assignee", "block_kind", "human_request", "current_run_id", "session_id")
_BLOCKING = frozenset({"blocked", "triage"})


def lineage_of(identity: dict[str, Any], tip: str) -> str:
    """The lineage root from a v1 identity (``owner_epoch = lineage:<root>``); the tip when unknown."""
    epoch = str(identity.get("owner_epoch") or "")
    return epoch[len("lineage:"):] if epoch.startswith("lineage:") and len(epoch) > len("lineage:") else tip


def is_remote(card: dict[str, Any]) -> bool:
    """A replica row (another gateway's card): ``_remote_cards`` rows carry their owner ``gateway``."""
    return bool(card.get("gateway"))


def board_paths() -> dict[str, Path]:
    from hermes_cli.kanban_session_activity import _board_db_paths, _board_slug

    return {_board_slug(path): path for path in _board_db_paths()}


def _json_list(values: Iterable[str]) -> str:
    return json.dumps(sorted(set(values)))


def _parents(conn: sqlite3.Connection, ids: set[str]) -> list[tuple[str, str]]:
    return [(str(r[0]), str(r[1])) for r in conn.execute(
        "SELECT parent_id, child_id FROM task_links WHERE child_id IN (SELECT value FROM json_each(?))",
        (_json_list(ids),))]


def _rows(conn: sqlite3.Connection, ids: set[str]) -> dict[str, dict[str, Any]]:
    from hermes_cli.kanban_table_columns import table_columns

    cols = table_columns(conn, "tasks")
    select = ", ".join(c if c in cols else f"NULL AS {c}" for c in _ROW_FIELDS)
    conn.row_factory = sqlite3.Row
    return {str(r["id"]): dict(r) for r in conn.execute(
        f"SELECT {select} FROM tasks WHERE id IN (SELECT value FROM json_each(?))", (_json_list(ids),))}


def block_episodes(conn: sqlite3.Connection, ids: set[str]) -> dict[str, str]:
    """``task id -> 'evt:<id>'`` of the lifecycle event that began each card's current blocked/triage episode."""
    from hermes_cli.kanban_triage_attention import LIFECYCLE_KINDS, _obj, landing

    if not ids:
        return {}
    phase: dict[str, Any] = {}
    start: dict[str, str] = {}
    for row in conn.execute(
            "SELECT id, task_id, kind, payload FROM task_events WHERE task_id IN (SELECT value FROM json_each(?)) "
            "AND kind IN (SELECT value FROM json_each(?)) ORDER BY id",
            (_json_list(ids), json.dumps(list(LIFECYCLE_KINDS)))):
        tid = str(row[1])
        landed = landing(row[2], _obj(row[3]))
        if landed is None:
            continue
        if landed in _BLOCKING and phase.get(tid) not in _BLOCKING:
            start[tid] = f"evt:{row[0]}"
        elif landed not in _BLOCKING:
            start.pop(tid, None)
        phase[tid] = landed
    return start


def local_graph(path: Path, linked: Iterable[str]) -> dict[str, Any]:
    """For the linked cards of one board: ``parents`` (child -> parent ids), closure-only ``rows`` (with triage
    entry clocks) and the block ``episodes`` of every blocked/triage card among them."""
    from hermes_cli.kanban_generation import open_ro
    from hermes_cli.kanban_triage_attention import annotate

    linked = {str(t) for t in linked}
    parents: dict[str, list[str]] = {}
    seen, frontier = set(linked), set(linked)
    with closing(open_ro(path, timeout=0.5)) as conn:
        for _depth in range(MAX_DEPTH):
            if not frontier or len(seen) >= MAX_CLOSURE:
                break
            found = set()
            for parent, child in _parents(conn, frontier):
                parents.setdefault(child, []).append(parent)
                if parent not in seen:
                    found.add(parent)
            room = max(0, MAX_CLOSURE - len(seen))
            frontier = set(sorted(found)[:room])
            seen |= frontier
        closure = seen - linked
        rows = _rows(conn, closure)
        annotate(conn, list(rows.values()))
        blocking = {t for t, r in rows.items() if r.get("status") in _BLOCKING} | linked
        episodes = block_episodes(conn, blocking)
    return {"parents": {k: sorted(v) for k, v in parents.items()}, "rows": rows, "episodes": episodes,
            "truncated": len(seen) >= MAX_CLOSURE}


def collect(db: Any, tip: str, *, trigger: str = "manual", concluding: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Every input :func:`hermes_session_state_reducer.reduce` needs for the lineage whose live tip is ``tip``."""
    import hermes_state_outcome as so
    from hermes_state_session_status_snapshot import collect_parts

    row = so._row(db, tip) or {}
    stored = so._parse(row.get("outcome_json")) or None
    parts = collect_parts(db, tip, stored, trigger=trigger, concluding=concluding, card_rows=True)
    errors: dict[str, str] = {}
    graphs: dict[str, Any] = {}
    paths = board_paths()
    by_board: dict[str, set[str]] = {}
    for card in parts.get("card_rows") or ():
        if card.get("board") in paths and not is_remote(card):
            by_board.setdefault(str(card["board"]), set()).add(str(card["id"]))
    for board, ids in sorted(by_board.items()):
        try:
            graphs[board] = local_graph(paths[board], ids)
        except Exception as exc:  # health: allow BLE001 -- an unreadable closure is unknown coverage, never empty
            logger.warning("session state: dependency closure unreadable on %s for %s", board, tip, exc_info=True)
            errors["closure"] = f"closure_read_failed:{type(exc).__name__}"
    stored_status = (stored or {}).get("status") if isinstance(stored, dict) else None
    return {"tip": tip, "lineage": lineage_of(parts["identity"], tip), "parts": parts, "graphs": graphs,
            "errors": errors, "stored_status": stored_status if isinstance(stored_status, dict) else None,
            "trigger": trigger}
