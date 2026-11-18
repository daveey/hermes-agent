"""Comment-id queries for the live comment bridge (moved out of ``kanban_db``; re-exported there)."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Optional


def writer_provenance(conn: sqlite3.Connection, task_id: str) -> dict:
    """``commented`` payload fields naming THIS process's run when it is the worker of ``task_id``
    (``HERMES_KANBAN_TASK`` matches and ``HERMES_KANBAN_RUN_ID`` is an int), else ``{}``.
    Every writer inside a worker run (the kanban_comment tool, ``hermes kanban comment``
    from its terminal, delegate children) inherits both, so the run is known in any process.

    [daveey] ``writer_run_id`` is database-local: sync remaps ``task_events.run_id`` but not
    this payload. ``writer_run_uid`` (``task_runs.uid``) names the writer on every replica."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return {}
    try:
        run_id = int((os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip())
    except ValueError:
        return {}
    uid = _run_uid(conn, run_id)
    return {"writer_run_id": run_id, **({"writer_run_uid": uid} if uid else {})}


def _run_uid(conn: sqlite3.Connection, run_id: int) -> Optional[str]:
    try:
        row = conn.execute("SELECT uid FROM task_runs WHERE id = ?", (int(run_id),)).fetchone()
    except sqlite3.OperationalError:
        return None  # unsynced schema: no uid column
    return row[0] if row and isinstance(row[0], str) and row[0] else None


def own_comment_ids(conn: sqlite3.Connection, task_id: str, run_id: int) -> set[int]:
    """Ids of the comments on ``task_id`` written from inside run ``run_id`` (any process).

    [daveey] A comment stamped with ``writer_run_uid`` is matched by that stable uid only
    (the payload integer is local to the database that wrote it); legacy uid-less comments
    keep the integer match."""
    uid = _run_uid(conn, run_id)
    rows = conn.execute(
        "SELECT json_extract(payload, '$.comment_id') FROM task_events "
        "WHERE task_id = ? AND kind = 'commented' AND ("
        "(json_type(payload, '$.writer_run_uid') IS NULL "
        " AND json_extract(payload, '$.writer_run_id') = ?) "
        "OR (? IS NOT NULL AND json_type(payload, '$.writer_run_uid') = 'text' "
        "    AND json_extract(payload, '$.writer_run_uid') = ?))",
        (task_id, int(run_id), uid, uid),
    ).fetchall()
    return {int(r[0]) for r in rows if r[0] is not None}


def delivered_comment_ids(conn: sqlite3.Connection, task_id: str, run_id: int) -> set[int]:
    """Ids the live bridge already steered into run ``run_id`` (``comment_delivered`` events)."""
    out: set[int] = set()
    for (raw,) in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'comment_delivered' "
            "AND run_id = ?", (task_id, int(run_id))).fetchall():
        try:
            out.update(int(i) for i in (json.loads(raw or "{}").get("comment_ids") or []))
        except (ValueError, TypeError, AttributeError):
            continue
    return out


def comment_id_before_run_start(conn: sqlite3.Connection, task_id: str, run_id: int) -> Optional[int]:
    """Highest comment id on ``task_id`` created before run ``run_id`` started (0 when none);
    None when the run or its ``started_at`` can't be resolved. Those earlier comments are in
    the worker's prompt, so the live comment bridge seeds its watermark here."""
    row = conn.execute("SELECT started_at FROM task_runs WHERE id = ? AND task_id = ?",
                       (int(run_id), task_id)).fetchone()
    if not row or row[0] is None:
        return None
    got = conn.execute("SELECT MAX(id) FROM task_comments WHERE task_id = ? AND created_at < ?",
                       (task_id, int(row[0]))).fetchone()
    return int(got[0]) if got and got[0] is not None else 0


def agent_comment_ids(conn: sqlite3.Connection, task_id: str, comment_ids: list[int]) -> set[int]:
    """Those of ``comment_ids`` whose ``commented`` event says an agent wrote them
    (``writer_kind='agent'``). Legacy and system comments carry no kind and are not included. [daveey]"""
    ids = [int(i) for i in comment_ids]
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT json_extract(payload, '$.comment_id') FROM task_events "
        "WHERE task_id = ? AND kind = 'commented' "
        "AND json_extract(payload, '$.writer_kind') = 'agent' "
        f"AND json_extract(payload, '$.comment_id') IN ({marks})", (task_id, *ids)).fetchall()
    return {int(r[0]) for r in rows if r[0] is not None}


def add_comment(conn: sqlite3.Connection, task_id: str, author: str, body: str, *,
                origin_gateway: Optional[str] = None, writer_kind: Optional[str] = None,
                writer_session: Optional[str] = None, relayed_human: Optional[dict] = None,
                system: bool = False) -> int:
    """Append a comment (moved from ``kanban_db``; re-exported there). ``origin_gateway``: the gateway it came from (the hermes-hub relay passes the
    sender); default: the gateway the author spelling names, else this gateway (kanban_comment_origin). [daveey]

    ``writer_kind`` ("agent" | "human") records who typed it on the ``commented`` event, with the
    agent's ``writer_session``: David and his agents share one profile, so the author can't say.
    Unset (system writers, legacy callers) leaves the payload as before. [daveey]

    ``relayed_human`` (``{session, message_id, at}``, verified by ``kanban_relayed_human``) marks an agent
    comment that carries the user's reply from a chat: the no-progress budget counts it as a human answer,
    while ``writer_kind`` stays ``agent`` so it gains no operator authority (kanban-d1b921f32e). [daveey]

    ``system``: a kernel note written for the card's worker, not by it (the readiness pass telling a repair card its
    child cleared). It carries no :func:`writer_provenance`, even when that pass runs inside the card's own worker,
    so the live comment bridge still delivers it to that run. [daveey]"""
    if relayed_human is not None and writer_kind != "agent":
        raise ValueError("relayed_human marks an agent's relay of the user's message; it needs writer_kind='agent'")
    if not body or not body.strip():
        raise ValueError("comment body is required")
    if not author or not author.strip():
        raise ValueError("comment author is required")
    if writer_kind not in (None, "agent", "human"):
        raise ValueError(f"writer_kind must be 'agent' or 'human', not {writer_kind!r}")
    from hermes_cli.kanban_comment_origin import resolve_origin
    origin = resolve_origin(author, origin_gateway)
    now = int(time.time())
    # [daveey] A comment written from inside a worker run records that run on its
    # ``commented`` event, so the live comment bridge can skip exactly the run's own notes
    # by id. Authorship cannot: workers and the chats steering them share a profile.
    # ``allow_nested=True``: graph builders (kanban_swarm blackboard seeding)
    # compose comment writes under one outer commit.
    with _kb.write_txn(conn, allow_nested=True):
        _kb._require_task(conn, task_id)
        if relayed_human:  # the card holder's own checks, atomic with the write (one relay per user turn)
            from hermes_cli.kanban_relayed_human import admit
            admit(conn, task_id, relayed_human)
        cur = conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at, origin_gateway) "
            "VALUES (?, ?, ?, ?, ?)", (task_id, author.strip(), body.strip(), now, origin),
        )
        cid = int(cur.lastrowid or 0)
        payload: dict = {"author": author, "len": len(body), "comment_id": cid}
        # Sync's insert trigger assigns the portable identity before this read.
        # Unsynced boards have no uid column; retain their local legacy form.
        row = conn.execute("SELECT * FROM task_comments WHERE id = ?", (cid,)).fetchone()
        if "uid" in row.keys() and row["uid"]:
            payload["comment_uid"] = row["uid"]
        if not system:
            payload.update(writer_provenance(conn, task_id))  # writer_run_id + portable writer_run_uid
        # [daveey] A worker commenting on ANOTHER card: name its own card, so that card's owner can wait on
        # "a comment from card X" (wait-comment --from-card). The author cannot say: workers share a profile.
        own = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
        if not system and own and own != task_id and re.fullmatch(r"t_[0-9a-f]{6,}", own):
            payload["writer_task"] = own
        if writer_kind:
            payload["writer_kind"] = writer_kind
            if writer_session:
                payload["writer_session"] = str(writer_session)
        if relayed_human:
            payload["relayed_human"] = dict(relayed_human)
        _kb._append_event(conn, task_id, "commented", payload)
        return cid


# Late-bound at the bottom (import-cycle breaking, like ``kanban_db_schedule``): ``kanban_db``
# re-exports from this module, so it must be fully populated before ``kanban_db`` imports it.
from hermes_cli import kanban_db as _kb
