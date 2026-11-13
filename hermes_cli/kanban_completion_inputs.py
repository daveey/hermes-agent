"""Batched, read-only inputs of the owner's completion verdict.

``kanban_origin_ledger.completion_verdict`` is the single verdict. This module only reads the rows it
needs for many cards at once inside the caller's transaction: at most six SELECTs per 500 cards (tasks,
latest completion, latest run, id-bound judge/acceptance events, the Board leases of runs whose completion
records no origin, and archive events only when an archived card is present), plus the board's cached
receipt epoch. It adds no store and never writes.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import NamedTuple

from hermes_cli.kanban_completion_refs import leased_elsewhere, pick, read_candidates
from hermes_cli.kanban_origin_ledger import _receipt_epoch, completion_verdict

logger = logging.getLogger(__name__)
CHUNK = 500


class Completion(NamedTuple):
    status: str | None
    verified: bool
    legacy: bool
    event_id: int | None
    event_run_id: int | None
    run_id: int | None
    run_uid: str | None
    assignee: str | None
    session_id: str | None


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _select(columns, prefix, wanted):
    return ",".join(f"{prefix}{c}" if c in columns else f"NULL AS {c}" for c in wanted)


def _rows(conn, sql, args):
    cursor = conn.execute(sql, args)
    cursor.row_factory = sqlite3.Row
    return cursor


def read_completions(conn, ids) -> dict[str, Completion]:
    """``{task_id: Completion}`` for every id that exists; the verdict fails closed per card."""
    ids = list(dict.fromkeys(ids))
    out: dict[str, Completion] = {}
    if not ids:
        return out
    task_cols, run_cols, event_cols = (_columns(conn, t) for t in ("tasks", "task_runs", "task_events"))
    if not task_cols or not event_cols or not run_cols:
        return out
    task_fields = _select(task_cols, "t.", ("id", "status", "completed_at", "current_run_id", "goal_mode",
                                            "completion_contract", "title", "body", "assignee", "session_id"))
    run_fields = _select(run_cols, "r.", ("id", "task_id", "uid", "status", "outcome", "ended_at", "summary"))
    event_fields = ",".join(("e.id", "e.task_id", "e.run_id", "e.kind", "e.created_at",
                             "e.payload", "e.origin_gateway" if "origin_gateway" in event_cols
                             else "NULL AS origin_gateway"))
    epoch = []
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start:start + CHUNK]
        marks = ",".join("?" for _ in chunk)
        tasks = {r["id"]: r for r in _rows(conn, f"SELECT {task_fields} FROM tasks t WHERE t.id IN ({marks})", chunk)}
        completed = {r["task_id"]: r for r in _rows(
            conn, f"SELECT * FROM (SELECT {event_fields}, ROW_NUMBER() OVER (PARTITION BY e.task_id ORDER BY e.id DESC) "
                  f"AS _rank FROM task_events e WHERE e.task_id IN ({marks}) AND e.kind='completed') WHERE _rank=1",
            chunk)}
        runs = {r["task_id"]: r for r in _rows(
            conn, f"SELECT * FROM (SELECT {run_fields}, ROW_NUMBER() OVER (PARTITION BY r.task_id ORDER BY r.id DESC) "
                  f"AS _rank FROM task_runs r WHERE r.task_id IN ({marks})) WHERE _rank=1", chunk)}
        archived_ids = [t for t, r in tasks.items() if r["status"] == "archived" and t in completed]
        archived = {}
        if archived_ids:
            amarks = ",".join("?" for _ in archived_ids)
            archived = {r["task_id"]: r for r in _rows(
                conn, f"SELECT * FROM (SELECT {event_fields}, ROW_NUMBER() OVER (PARTITION BY e.task_id "
                      f"ORDER BY e.id DESC) AS _rank FROM task_events e WHERE e.task_id IN ({amarks}) "
                      "AND e.kind='archived') WHERE _rank=1", archived_ids)}
        refs = read_candidates(conn, list(completed))
        imported = {tid: row["origin_gateway"] if isinstance(row["origin_gateway"], str) and row["origin_gateway"]
                    else None for tid, row in completed.items()}
        # [daveey] Replayed before origins were recorded: the run's Board lease names the gateway.
        legacy = leased_elsewhere(conn, {t: completed[t]["run_id"] for t, src in imported.items() if src is None})
        imported.update(legacy)
        for tid, task in tasks.items():
            out[tid] = _completion(conn, tid, task, completed.get(tid), runs.get(tid), archived.get(tid),
                                   refs.get(tid, []), imported.get(tid), tid in legacy, epoch)
    return out


def _payload(raw):
    """Decoded event payload; malformed payloads are not evidence (fail closed). Never size-capped: the\n    owner verdict reads the whole payload, so the batch must too."""
    if raw is None:
        return {}
    if not isinstance(raw, str):
        raise ValueError("completion payload unavailable")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("completion payload is not an object")
    return value


def _completion(conn, tid, task, event, run, archived, receipts, imported_from, legacy_import, epoch):
    row = (task["status"], task["completed_at"], task["current_run_id"], task["goal_mode"],
           task["completion_contract"])
    event_t = None if event is None else (event["id"], event["run_id"], event["created_at"], event["payload"])
    run_t = None if run is None else (run["id"], run["status"], run["outcome"], run["ended_at"], run["summary"])
    archived_t = None if archived is None else (archived["id"], archived["payload"])

    def bound_event(kind, event_id, before, uid=None):
        return pick(receipts, task_id=tid, kind=kind, event_id=event_id, uid=uid, before=before,
                    run_id=None if event is None else event["run_id"], imported_from=imported_from,
                    legacy=legacy_import)

    def receipt_epoch():
        if not epoch:
            epoch.append(_receipt_epoch(conn))
        return epoch[0]

    try:
        verified, legacy, _terminal = completion_verdict(
            tid, row, event_t, run_t, archived_t, bound_event=bound_event,
            current_task=lambda: (task["title"], task["body"]), receipt_epoch=receipt_epoch)
    except Exception:  # any one card's bad evidence is unverified, never a failed batch
        logger.debug("completion verdict input unreadable for %s; treated as unverified", tid, exc_info=True)
        verified = legacy = False
    return Completion(task["status"], verified, legacy, None if event is None else event["id"],
                      None if event is None else event["run_id"], None if run is None else run["id"],
                      None if run is None else run["uid"], task["assignee"], task["session_id"])


def read_verdicts(conn, ids) -> dict[str, tuple[bool, bool]]:
    """``{task_id: (verified, legacy)}``, the same verdict ``observe_owner_task`` publishes."""
    return {tid: (c.verified, c.legacy) for tid, c in read_completions(conn, ids).items()}
