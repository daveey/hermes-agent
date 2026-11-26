"""File-only evidence of a real preclaim abort; never re-enter the board writer."""
from __future__ import annotations

from contextlib import closing
import logging
import sqlite3
import time

CENSUS_SECONDS = .2
GOVERNOR = "storage_admission"
LABEL = "Storage admission"
SOURCE = "hermes_cli/kanban_dispatch_storage.py:run"
DETAIL = "Preclaim aborted by storage contention; candidate rules were not observed."


def _census(path, review, limit):
    from hermes_cli.kanban_lockguard import guard, guarded_factory
    from hermes_cli.sqlite_safe_read import connect_tracked

    deadline = time.monotonic() + CENSUS_SECONDS
    # The caller already owns the native lifecycle/dispatch identity. A general
    # opener would run recovery and could write: this bounded observer must not.
    with closing(connect_tracked(path.as_uri() + "?mode=ro", uri=True, timeout=CENSUS_SECONDS,
                                 factory=guarded_factory(), wait_for_tracking=False)) as reader:
        guard(reader, strict=True)
        reader.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        reader.execute("BEGIN")  # read transaction: census and coverage share one WAL view
        statuses = ("ready", "review") if review else ("ready",)
        marks = ",".join("?" for _ in statuses)
        rows = reader.execute(
            f"SELECT id, status FROM tasks WHERE status IN ({marks}) AND claim_lock IS NULL "
            "ORDER BY created_at, id LIMIT ?", (*statuses, limit + 1)).fetchall()
        # Excluded status coverage is explicit, not an inferred zero-hold reading.
        counts = dict(reader.execute("SELECT status, count(*) FROM tasks GROUP BY status"))
        if time.monotonic() >= deadline:
            raise TimeoutError("storage census deadline")
        return rows, counts


def record(conn, *, board, dry_run=False):
    """Called only for a typed storage abort under the validated owner lock."""
    from hermes_cli import kanban_governors as governors
    from hermes_cli.kanban_dispatch_owner import connection_db_path
    from hermes_cli.kanban_db_dispatch import review_dispatch_enabled
    from hermes_cli.kanban_suspend_ledger import _configured_interval_seconds

    governors._take_capture(conn)  # discard partial/exceptional tick capture
    if dry_run:
        return None
    try:
        path = connection_db_path(conn)
        target = path.with_name(path.name + governors.SUFFIX)
        previous = governors._read(target) or {}
        error = None
        try:
            rows, counts = _census(path, review_dispatch_enabled(), governors.MAX_CARDS)
        except (OSError, sqlite3.Error, TimeoutError) as exc:
            rows, counts, error = [], None, type(exc).__name__
        now = time.time()  # new abort observation, never old per-card decisions
        known = {"governor": GOVERNOR, "label": LABEL, "scope": None, "source": SOURCE,
                 "threshold": "dispatch preclaim write-wait budget", "detail": DETAIL,
                 "since": now, "phase": "preclaim_aborted"}
        cards = {tid: {**known, "status": status, "overlaps": None, "overlaps_unknown": []}
                 for tid, status in rows[:governors.MAX_CARDS]}
        globals_ = [{**known, "verdict": "stop", "reached": True}]
        globals_ += [{"governor": gid, "verdict": "unknown", "reached": False, "detail": DETAIL}
                     for gid in governors.GLOBALS]
        readings = [{"governor": g["id"], "label": g["label"], "kind": g["kind"], "scope": None,
                     "active": None, "cards": None, "card_ids": [], "since": None,
                     "unknown": True} for g in governors.registry() if g["id"] != GOVERNOR]
        readings.append({**known, "kind": governors.HOLD, "active": True, "cards": len(cards),
                         "card_ids": list(cards)[:governors.MAX_IDS]})
        doc = {"version": governors.VERSION, "board": governors._board(conn), "tick_at": now,
               "phase": "preclaim_aborted", "scheduling_admitted": False,
               "global_stop": GOVERNOR, "globals": globals_, "governors": readings, "cards": cards,
               "caps": {}, "idle_total": None, "charged_total": len(cards) if counts is not None else None,
               "truncated": len(rows) > governors.MAX_CARDS,
               "coverage": {"complete": False, "excluded_statuses": ["triage"],
                            "status_counts": counts, "census_error": error}}
        doc.update(governors.completion_policy(previous, now, _configured_interval_seconds()))
        if connection_db_path(conn) != path:
            return None
        governors._write(target, doc)
        return doc
    except Exception:  # health: allow BLE001 -- evidence cannot change the real storage refusal
        logging.getLogger(__name__).debug("storage admission observation unavailable", exc_info=True)
        return None
