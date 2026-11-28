"""Lean listed-card reader for session outcome. Sidebar ``session_tasks`` stays full.

Same local selection (creator/subscriber union, creator precedence, counted-sub
clause, watch exclusion, ``_TASK_FIELDS``) and the same remote membership/dedup/
wait derivation as ``session_tasks``. Skips execution assemble, done receipts,
card details and ``add_cards``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

_log = logging.getLogger("hermes_cli.kanban_session_activity")


def select_listed_local_tasks(conn: sqlite3.Connection, ids: list[str], statuses: tuple[str, ...],
                              ) -> dict[str, dict[str, Any]]:
    """Legacy union view: creator precedence across the supplied lineage."""
    return select_listed_local_task_set(conn, ids, statuses, strict=False)[0]


def select_listed_local_task_set(conn: sqlite3.Connection, ids: Iterable[str], statuses: tuple[str, ...], *, strict: bool = True,
                                 ) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], str]]:
    """Return canonical cards AND complete ``(session, task) -> via`` membership.

    JSON scopes use two bindings regardless of lineage size. Creator precedence
    applies per pair as well as to the legacy union. The caller owns row_factory
    and transaction lifetime; this selector neither opens nor closes a connection.
    Missing legacy subscriptions are supported; other subscriber SQL errors
    propagate rather than certifying partial coverage. Only the historical union
    wrapper opts into ``strict=False`` to retain its optional-read boundary.
    """
    from hermes_cli.kanban_session_activity import _TASK_FIELDS, _counted_sub_clause
    from hermes_cli.kanban_watch import not_watch_sql as _not_watch

    args = (json.dumps(list(dict.fromkeys(ids))), json.dumps(statuses))
    sid_ph = "SELECT value FROM json_each(?)"
    st_ph = "SELECT value FROM json_each(?)"
    from hermes_cli.kanban_table_columns import table_columns
    cols = table_columns(conn, "tasks")  # one result row, not one per column
    if "session_id" not in cols:
        return {}, {}  # explicitly supported pre-session schema
    select = ", ".join(f"t.{c}" if c in cols else f"NULL AS {c}" for c in _TASK_FIELDS)
    run_start = "NULL"
    if "current_run_id" in cols and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_runs'").fetchone():
        run_start = "(SELECT r.started_at FROM task_runs r WHERE r.id = t.current_run_id AND r.task_id = t.id)"
    found: dict[str, dict[str, Any]] = {}
    pairs: dict[tuple[str, str], str] = {}
    watch = _not_watch(conn)
    for row in conn.execute(
            f"SELECT {select}, {run_start} AS current_run_started_at FROM tasks t "
            f"WHERE t.session_id IN ({sid_ph}) AND t.status IN ({st_ph}) AND {watch}", args):
        found[row["id"]] = {**dict(row), "via": "created"}
        pairs[(row["session_id"], row["id"])] = "created"
    from hermes_cli.kanban_local_chat_ownership import created_rows
    for row in created_rows(
            conn, f"{select}, {run_start} AS current_run_started_at, l.chat_session AS _member_session",
            f"l.chat_session IN ({sid_ph}) AND t.status IN ({st_ph}) AND {watch}", args):
        info = dict(row)
        sid = info.pop("_member_session")
        found.setdefault(row["id"], {**info, "via": "created"})
        pairs[(sid, row["id"])] = "created"
    try:
        for row in conn.execute(
                f"SELECT {select}, {run_start} AS current_run_started_at, s.chat_id AS _member_session "
                f"FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
                f"WHERE s.platform = 'tui' AND {_counted_sub_clause(conn)} "
                f"AND s.chat_id IN ({sid_ph}) AND t.status IN ({st_ph}) AND {watch}", args):
            info = dict(row)
            sid = info.pop("_member_session")
            found.setdefault(row["id"], {**info, "via": "subscribed"})
            pairs.setdefault((sid, row["id"]), "subscribed")
    except sqlite3.Error:  # board predates notify subscriptions
        if strict and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_notify_subs'").fetchone():
            raise
    from hermes_cli.kanban_triage_attention import annotate
    annotate(conn, found.values())
    return found, pairs


def append_remote_listed_cards(out: list[dict[str, Any]], ids: list[str], statuses: tuple[str, ...],
                               stale_ok: bool) -> None:
    """Remote cards this lineage owns: session or origin_session, key-deduped, wait derived first."""
    from hermes_cli.kanban_session_activity import _remote_cards, _remote_wait
    from hermes_cli.kanban_session_activity_release import PENDING_RELEASE_STATUSES

    wanted, seen = set(ids), set()
    remote: list[dict[str, Any]] = []
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
            remote.append(row)
    from hermes_cli.kanban_session_activity_card_details import annotate_remote_upstream
    annotate_remote_upstream(remote)


def _read_lean_session_tasks(path: Path, ids: list[str], statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    from hermes_cli.kanban_session_activity import _board_slug, _execution_transaction
    from hermes_cli.kanban_waiting_reader import reading
    from hermes_cli.kanban_session_wait_rows import _local_wait_info

    # Waiting-evidence custody is separate from sidebar enrichment. Its bounded
    # retained reader keeps no snapshot or answers; concurrent/nested borrows use
    # the original private guarded admission and close path without waiting.
    with reading(path, busy_budget=0.5) as conn, _execution_transaction(conn):
        conn.row_factory = sqlite3.Row
        found = select_listed_local_tasks(conn, ids, statuses)
        from hermes_cli.kanban_retained_wait import annotate as _retained_wait
        _retained_wait(conn, found)  # a retained triage card an open parent or recorded wait explains is no ask
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
