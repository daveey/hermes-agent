"""Lean listed-card reader for session outcome. Sidebar ``session_tasks`` stays full.

Same local selection (creator/subscriber union, creator precedence, counted-sub
clause, watch exclusion, ``_TASK_FIELDS``) and the same remote membership/dedup/
wait derivation as ``session_tasks``. Skips execution assemble, done receipts,
card details and ``add_cards``.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

_log = logging.getLogger("hermes_cli.kanban_session_activity")


def select_listed_local_tasks(conn: sqlite3.Connection, ids: list[str], statuses: tuple[str, ...],
                              ) -> dict[str, dict[str, Any]]:
    """Tasks on one board that any of ``ids`` created or tui-shepherds, in ``statuses``.

    Creator rows win over subscriber rows (``setdefault``). Same pairs ``_read_counts`` counts.
    """
    from hermes_cli.kanban_session_activity import _TASK_FIELDS, _counted_sub_clause
    from hermes_cli.kanban_watch import not_watch_sql as _not_watch

    sid_ph = ",".join("?" for _ in ids)
    st_ph = ",".join("?" for _ in statuses)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "session_id" not in cols:
        return {}  # explicitly supported pre-session schema
    select = ", ".join(f"t.{c}" if c in cols else f"NULL AS {c}" for c in _TASK_FIELDS)
    run_start = "NULL"
    if "current_run_id" in cols and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_runs'").fetchone():
        run_start = "(SELECT r.started_at FROM task_runs r WHERE r.id = t.current_run_id AND r.task_id = t.id)"
    found: dict[str, dict[str, Any]] = {}
    watch = _not_watch(conn)
    for row in conn.execute(
            f"SELECT {select}, {run_start} AS current_run_started_at FROM tasks t "
            f"WHERE t.session_id IN ({sid_ph}) AND t.status IN ({st_ph}) AND {watch}", (*ids, *statuses)):
        found[row["id"]] = {**dict(row), "via": "created"}
    try:
        for row in conn.execute(
                f"SELECT {select}, {run_start} AS current_run_started_at FROM kanban_notify_subs s "
                f"JOIN tasks t ON t.id = s.task_id WHERE s.platform = 'tui' AND {_counted_sub_clause(conn)} "
                f"AND s.chat_id IN ({sid_ph}) AND t.status IN ({st_ph}) AND {watch}", (*ids, *statuses)):
            found.setdefault(row["id"], {**dict(row), "via": "subscribed"})
    except sqlite3.Error:  # board predates notify subscriptions
        pass
    return found


def append_remote_listed_cards(out: list[dict[str, Any]], ids: list[str], statuses: tuple[str, ...],
                               stale_ok: bool) -> None:
    """Remote cards this lineage owns: session or origin_session, key-deduped, wait derived first."""
    from hermes_cli.kanban_session_activity import _remote_cards, _remote_wait
    from hermes_cli.kanban_session_activity_release import PENDING_RELEASE_STATUSES

    wanted, seen = set(ids), set()
    for card in _remote_cards(ids, statuses, stale_ok):
        belongs = card["session"] in wanted or card.get("origin_session") in wanted
        listed = (card.get("status") in statuses
                  or (card.get("status") in PENDING_RELEASE_STATUSES and card.get("unfinished")))
        if belongs and listed and card["key"] not in seen:
            seen.add(card["key"])
            wait = _remote_wait(card)  # before trimming source fields; stale remote has none
            row = {**card, "session_id": card["session"]}
            if wait is not None:
                row["wait"] = wait
            out.append(row)


def _read_lean_session_tasks(path: Path, ids: list[str], statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    from hermes_cli.kanban_session_activity import _board_slug, _execution_transaction, cached_ro
    from hermes_cli.kanban_session_wait_rows import _local_wait_info

    with cached_ro(path) as conn, _execution_transaction(conn):
        conn.row_factory = sqlite3.Row
        found = select_listed_local_tasks(conn, ids, statuses)
        try:
            for tid, info in _local_wait_info(conn, found).items():
                found[tid]["wait"] = info
        except Exception as exc:  # health: allow BLE001 -- Same optional wait boundary as full sidebar reader; never drop listed cards on enrichment failure.
            _log.debug("kanban wait info read failed for %s: %s", path, exc, exc_info=True)
    board = _board_slug(path)
    return [{**t, "board": board} for t in found.values()]


def session_outcome_tasks(session_ids: Iterable[str], statuses: tuple[str, ...] | None = None, *,
                          stale_ok: bool = False) -> list[dict[str, Any]]:
    """Open/blocked/review cards the lineage created or shepherds, without sidebar enrichment."""
    from hermes_cli.kanban_session_activity import LISTED_STATUSES, _board_db_paths, _busy

    if statuses is None:
        statuses = LISTED_STATUSES
    ids = [s for s in dict.fromkeys(str(s) for s in session_ids if s)]
    if not ids:
        return []
    out: list[dict[str, Any]] = []
    deadline = _busy.request_deadline()
    for path in _board_db_paths():
        out.extend(_busy.session_tasks(path, ids, statuses, _read_lean_session_tasks, deadline=deadline,
                                       memo_namespace="lean"))
    for t in out:
        t.setdefault("key", f"{t['board']}/{t['id']}")
    append_remote_listed_cards(out, ids, statuses, stale_ok)
    return out
