"""Dashboard lifecycle writes on a card another machine runs under its live Board lease. [daveey]

The Board fences every lifecycle column of such a card to the lease holder (``live_claim``). A drag to
done/blocked/ready, a bulk status change or a reclaim from this dashboard used to commit locally and answer 200; the
Board then rejected the status write while the unfenced task_runs/task_events rows leaked to it. Each is now refused
before it writes, with the one ``kanban_remote_stop.refusal`` message the CLI verbs and kanban_* tools print.
Archive is the exception: it sends the holder a stop request (:func:`archive`).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from fastapi import HTTPException

from hermes_cli.kanban_remote_stop import RemoteArchiveRefused, refusal, remote_holder
from hermes_cli.kanban_remote_stop import archive as _remote_archive

# Dashboard status -> the verb named in the refusal. ready/todo/triage from ``running`` close the run as reclaimed.
_STATUS_VERBS = {"done": "complete", "blocked": "block", "scheduled": "schedule", "review": "request review",
                 "ready": "reclaim", "todo": "reclaim", "triage": "reclaim"}


def patch_verb(payload: Any) -> Optional[str]:
    """The fenced verb a PATCH /tasks/{id} or bulk body asks for, else None (archive, rejected or no status)."""
    if payload.status is not None and not getattr(payload, "archive", False):
        return _STATUS_VERBS.get(payload.status)
    if getattr(payload, "reclaim_first", False) and payload.assignee is not None:
        return "reassign"
    return None


def lease_refusal(conn: sqlite3.Connection, task_id: str, verb: Optional[str]) -> Optional[dict]:
    """``{message, holder, rejection, code}`` when ``verb`` would write a card under another machine's live lease."""
    holder = remote_holder(conn, task_id) if verb else None
    if holder is None or verb is None:
        return None
    return {"message": refusal(verb, task_id, holder), "holder": holder, "rejection": "live_claim",
            "code": "remote_live_lease"}


def refuse_live_lease(conn: sqlite3.Connection, task_id: str, verb: Optional[str]) -> None:
    """409 before any write when ``verb`` targets a card another machine runs under its live Board lease."""
    if (detail := lease_refusal(conn, task_id, verb)) is not None:
        raise HTTPException(status_code=409, detail=detail)


def archive(conn: sqlite3.Connection, task_id: str) -> tuple[bool, Optional[str]]:
    """Archive, or send a stop request when another machine runs the card under its live Board lease
    (``kanban_remote_stop``): ``(ok, holder)``, holder set when the archive is pending there."""
    try:
        out = _remote_archive(conn, task_id)
    except RemoteArchiveRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return bool(out), (out or {}).get("holder") if (out or {}).get("status") == "archive_pending" else None
