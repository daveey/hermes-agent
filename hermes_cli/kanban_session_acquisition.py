"""Canonical listed-card enrichment on caller-owned SQLite snapshots. [daveey]

No connection, transaction, migration or persistent cache is owned here. Optional
foreign-parent evidence must be captured by the caller; None means local only.
Execution history/window scans and presentation history remain canonical, not a
bounded-work guarantee. Membership pairs retain per-chat provenance before union
creator precedence; consumers must apply that provenance to each chat separately.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from hermes_cli.kanban_session_activity_card_details import LEGACY_REPLICA


def enrich_session_tasks(conn: sqlite3.Connection, path: Path, found: dict[str, dict[str, Any]], *,
                         observed_at: float, scope: tuple[str | None, str | None], replica: Any = None) -> None:
    """Mutate selected cards using the single legacy authority/detail/wait policy.

    ``LEGACY_REPLICA`` preserves legacy shadow acquisition and closure. An explicit
    connection or None borrows only the supplied inputs, never opens or closes a
    shadow. Caller supplies sqlite3.Row and owns all transaction lifetimes.
    """
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli.kanban_execution_source import assemble
    from hermes_cli.kanban_execution_observations import ADAPTER_ERRORS
    from hermes_cli.kanban_primary_execution import PrimaryMirror, attach

    board = activity._board_slug(path)
    gateway, profile = scope
    for info in found.values():
        info['_execution_scope'] = {'gateway': gateway, 'profile': profile, 'board': board}
    primary = False
    try:
        activity._require_local_execution_authority(conn)
        authority = True
    except PrimaryMirror:
        authority, primary = False, True
    except ADAPTER_ERRORS:
        authority = False
    try:
        if primary:
            attach(conn, found, board=board, gateway=gateway, profile=profile)
            for info in found.values():
                if '_replica_run' not in info:
                    info['_execution_error'] = True
        elif authority:
            sources = assemble(conn, found, board=board, gateway=gateway, profile=profile,
                               observed_at=observed_at)
            for tid, source in sources.items():
                found[tid]['_execution_source'] = source
        else:
            raise RuntimeError('local execution authority unavailable')
    except ADAPTER_ERRORS:
        activity._log.debug('kanban execution source failed for %s', path, exc_info=True)
        for info in found.values():
            info['_execution_error'] = True
    if authority:
        activity._receipts.attach_done(conn, found, board=board, gateway=gateway, profile=profile,
                                      observed_at=observed_at)
    if replica is LEGACY_REPLICA:
        activity._add_card_details(conn, found)
    else:
        activity._add_card_details(conn, found, replica=replica)
    try:
        # Legacy age policy uses the current clock; owned captures freeze it.
        waits = (activity._local_wait_info(conn, found) if replica is LEGACY_REPLICA
                 else activity._local_wait_info(conn, found, now=observed_at, replica=replica))
        for tid, info in waits.items():
            found[tid]['wait'] = info
    except Exception as exc:  # health: allow BLE001 -- Preserve the legacy optional wait boundary; listed cards remain on display failure.
        activity._log.debug('kanban wait info read failed for %s: %s', path, exc)


def read_session_task_set(conn: sqlite3.Connection, path: Path, session_ids: Iterable[str],
                          statuses: tuple[str, ...], *, observed_at: float, scope: tuple[str | None, str | None],
                          replica: sqlite3.Connection | None = None,
                          ) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], str]]:
    """Return enriched union cards and complete ``(session, task) -> via`` pairs.

    Must run inside the caller's query-only transaction with sqlite3.Row. ``path``
    is scope metadata only, never opened here. ``replica`` is optional captured
    foreign-parent input, borrowed without closure; explicit None is local only.
    Resolve ``scope`` with canonical ``card_scope(path)`` BEFORE entering capture:
    cold gateway identity discovery can otherwise open an unrelated identity DB.
    This does not attest that two supplied transactions share an atomic snapshot.
    """
    from hermes_cli.kanban_session_activity import _board_slug
    from hermes_cli.kanban_session_outcome_tasks import select_listed_local_task_set

    found, pairs = select_listed_local_task_set(conn, session_ids, statuses)
    enrich_session_tasks(conn, path, found, observed_at=observed_at, scope=scope, replica=replica)
    for info in found.values():
        info['board'] = _board_slug(path)
    return found, pairs
