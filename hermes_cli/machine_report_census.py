"""The metrics report's census of running card runs, on the sync loop's kept board readers. [daveey]

``machine_metrics.active_card_runs`` opens a fresh read-only connection per board on every call, and each fresh
connection parses that board's whole schema before its first statement. The sync loop's report runs it every
``REPORT_EVERY_S``: on a 10-board host that was the report's whole cost (David-MBP 2026-11-21, idle gateway:
``sync.step/...kanban_sync_timing.report`` 0.257 s of CPU over 12 reports in 120 s, 2.28 ms per sync round; a
10-board fixture measures 23 ms per census, 22 ms of it in the first statement of the ten fresh connections).

:func:`census` answers the same question from ``BoardWatch`` readers the loop already keeps open for
``outbox_pending``. Per board it reuses the stored running rows while that reader's ``data_version`` is unchanged:
a real change key (any other connection's commit moves it; a replaced or restored file reopens the reader), never
a timer. The rows are a pure function of the board file (no clock, no config), and pool counts are recomputed from
them on every call.

Only the report uses this. Admission (``kanban_admission``, grant-echo exclusion, owner output, an explicit board
inventory) keeps calling ``active_card_runs`` and its fresh per-call snapshot. Anything this reader cannot answer
exactly the way that function's one-statement path does (a pre-receipt schema, no JSON1, lease holders without
sync state, a board that cannot be read) falls back to ``active_card_runs`` for the whole census, so such hosts
report exactly what they did before.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Callable, Optional

TAG = "machine_report_census.running_rows"


def census(watch: Any, fallback: Optional[Callable[..., Optional[int]]] = None) -> Callable[..., Optional[int]]:
    """``count(detail=None)`` for ``machine_admission_metrics.collect``, reading through *watch*."""

    def count(detail: Optional[dict] = None) -> Optional[int]:
        from hermes_cli import kanban_db as kb
        from hermes_cli import machine_metrics as mm
        from hermes_cli.kanban_db_boards import named_board_db_files
        from hermes_cli.kanban_sync_cpu import detail as diagnostic

        uncached = fallback or mm.active_card_runs
        try:
            default = kb.kanban_home() / "kanban.db"
            total = 0
            receipts: list = []
            seen = set()
            for path in (default, *(p for _, p in diagnostic("collect.census.boards", named_board_db_files))):
                try:
                    stat = diagnostic("collect.census.stat", path.stat)
                except FileNotFoundError:
                    if path == default:
                        continue  # a never-created default board holds no runs
                    raise  # a vanished named board invalidates the sample, as in active_card_runs
                identity = (stat.st_dev, stat.st_ino)
                if identity in seen:
                    continue
                seen.add(identity)
                got = diagnostic("collect.census.read",
                                 lambda: watch.read(path, TAG, mm._running_rows, unchanged_ok=True))
                if got is None or got[1] is None:
                    return diagnostic("collect.census.fallback", lambda: uncached(detail=detail))
                synced, own_id, rows = got[1]
                if synced and not own_id:
                    return None
                total += len(rows)
                receipts.extend(row[1:] for row in rows)
            if detail is not None:
                if "pools" in detail:
                    from hermes_cli.kanban_admission_pools import count_pools
                    pools = diagnostic("collect.census.pools",
                                       lambda: count_pools(receipts, origins=detail.get("capacity_origins")))
                    for pool, n in pools.items():
                        detail["pools"][pool] += n
                detail["excluded"] = 0
            return total
        except (OSError, sqlite3.Error, ValueError):
            return None

    return count
