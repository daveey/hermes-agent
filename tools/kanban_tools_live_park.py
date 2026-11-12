"""Refuse an outside park of a card a live worker owns (t_01a1d01a0a065d6b). [daveey]

Incident 2026-11-12: an orchestrating chat called ``kanban_schedule(task_id=...)`` on a card worker-2 had claimed
3 s earlier. ``schedule_task`` without ``expected_run_id`` closed the card's open run under the worker; the
worker found its run ended, exited rc=0 without a terminal kanban call, and the dispatcher booked a protocol
violation, re-queued the card and sent the chat a failure notice.

Choice: refuse (option a), not queue the park for the worker (option b). The worker owns its run, and a refusal
keeps that single-writer rule: nothing is ended under a live process, so its clean exit is never misread as a
failure. The steer the refusal names already exists: a ``kanban_comment`` reaches a live worker as an
out-of-band message at its next model call, and the worker parks its own card. A queued park would need a new
pending-park store, a check in every worker tool call, and expiry rules for a worker that never calls a tool.

A card counts as live-claimed when it is ``running`` and one of these holds:

- this host's claim and its worker process is alive (``kanban_db._claim_is_live``);
- another gateway holds an unexpired Board lease (``kanban_db_sync.foreign_live_lease``);
- the Board's ``claimed`` event for the card's open current run names another gateway and has not expired, while
  the lease mirror has not arrived yet. This was the incident: the creator's copy had the run and the claim
  event but no mirrored lease, so the existing ``remote_live_lease`` refusal did not fire.

Applies only to a caller that does not own the run (no ``HERMES_KANBAN_TASK``/``HERMES_KANBAN_RUN_ID`` for the
card): a worker parking its own card is unaffected, and so is a ready, todo or blocked card.

The CLI (``hermes kanban schedule|block <id>``) is a sibling entry path; it refuses through ``cli_refusal``, called
from ``hermes_cli.kanban_cli_park._guarded_park`` (t_01a1d0622df6ea87). The check stays out of
``kanban_db.schedule_task``/``block_task``, so internal parkers (owner-down, placement forwarding) are unchanged.
"""
from __future__ import annotations

import json
import time
from typing import Optional

CODE = "live_claim_park"
FENCED_TOOLS = frozenset({"kanban_schedule", "kanban_block"})


def live_claim(conn, task_id: str, now: Optional[float] = None) -> Optional[dict]:
    """``{holder, run_id, via[, pid]}`` when a live worker owns ``task_id``'s current run, else None."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    row = conn.execute("SELECT status, claim_lock, worker_pid, worker_started_at, current_run_id FROM tasks "
                       "WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["status"] != "running":
        return None
    run_id = row["current_run_id"]
    if kb._claim_is_live(row):
        return {"holder": row["claim_lock"], "pid": row["worker_pid"], "run_id": run_id, "via": "local_worker"}
    holder = sync.foreign_live_lease(conn, task_id, now)
    if holder:
        return {"holder": holder, "run_id": run_id, "via": "board_lease"}
    if run_id is None or row["claim_lock"] is not None or sync.board_mode(conn) != "primary":
        return None
    run = conn.execute("SELECT ended_at FROM task_runs WHERE id = ? AND task_id = ?", (run_id, task_id)).fetchone()
    if run is None or run["ended_at"] is not None:
        return None
    claim = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'claimed' "
        "AND json_valid(payload) AND json_extract(payload, '$.holder') IS NOT NULL ORDER BY id DESC LIMIT 1",
        (task_id, run_id)).fetchone()
    if claim is None:
        return None
    payload = json.loads(claim[0])
    holder, expires = payload.get("holder"), payload.get("expires")
    own = str(sync._state(conn)["gateway_id"])
    now_ms = int((time.time() if now is None else now) * 1000)
    if not holder or holder == own or not isinstance(expires, (int, float)) or int(expires) <= now_ms:
        return None
    return {"holder": str(holder), "run_id": run_id, "via": "board_claim"}


def _message(name: str, task_id: str, claim: dict, steer: str, stop: str) -> str:
    """The refusal text both entry paths print: holder, run, nothing changed, how to steer or stop the worker."""
    return (f"{name} refused: {task_id} is running under a live worker claim ({claim['holder']}, run "
            f"{claim['run_id']}). Parking it from here would end that run underneath the worker, whose clean exit "
            f"is then booked as a failure. Nothing changed. Steer the worker instead: {steer} reaches it at its next "
            f"model call, and it parks its own card. {stop}")


def refusal(conn, tool_name: str, task_id: str) -> Optional[str]:
    """A typed ``live_claim_park`` tool error when ``task_id`` holds a live claim, else None. Nothing changes."""
    claim = live_claim(conn, task_id)
    if claim is None:
        return None
    from tools.registry import tool_error
    return tool_error(
        _message(tool_name, task_id, claim,
                 f"kanban_comment(task_id='{task_id}', body='<what to do, e.g. park with "
                 "kanban_schedule(awaiting=...) and end the run>')",
                 "To stop the run outright use kanban_archive."),
        code=CODE, rejection="live_claim", task_id=task_id, holder=claim["holder"], run_id=claim["run_id"],
        claim_via=claim["via"], handoff_complete=False)


def cli_refusal(conn, op: str, task_id: str) -> Optional[str]:
    """The same refusal for ``hermes kanban schedule|block <id>`` from a caller that does not own the run
    (t_01a1d0622df6ea87): one stderr line for ``_bulk_apply``, else None. Nothing changes."""
    claim = live_claim(conn, task_id)
    if claim is None:
        return None
    return _message(
        f"kanban {op}", task_id, claim,
        f"hermes kanban comment {task_id} \"<what to do, e.g. park with kanban schedule and end the run>\"",
        f"To stop the run outright use hermes kanban reclaim {task_id} or hermes kanban archive {task_id}. "
        f"[{CODE}, via {claim['via']}]")


def guard_refusal(board, args: dict, tool_name: str, task_id: str) -> Optional[str]:
    """``refusal`` for an outside caller of a fenced tool (``_worker_guard``); None for anything else."""
    if tool_name not in FENCED_TOOLS:
        return None
    with board(args.get("board")) as (_kb, conn):
        return refusal(conn, tool_name, task_id)
