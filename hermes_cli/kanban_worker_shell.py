"""Prove a CLI caller is the terminal shell of a card's own dispatched worker run. [daveey]

A dispatched worker's terminal shell carries ``HERMES_KANBAN_WORKER_TASK`` and ``HERMES_SESSION_ID`` but not
``HERMES_KANBAN_TASK`` / ``HERMES_KANBAN_RUN_ID``, so ``hermes kanban complete`` from it (the kanban-worker CLI
fallback when a kanban tool hits a DB error) looked like an outside caller. Its completion closes the card's own
claimed run, whose ``owner_admitted`` session is that worker's: exactly the record ``hermes-core-wt ship`` accepts
as a review (latest run completed, one recorded reviewer session).

The env var alone is never trusted (any chat can export it). ``run_for`` returns the run id only when all hold:

- ``HERMES_KANBAN_WORKER_TASK`` names the card;
- the card is ``running`` under a claim, and its ``current_run_id`` run is open and ``running``;
- this process descends from that run's live worker: the ``worker_bound`` interpreter of the run, or the
  card's spawned ``worker_pid`` with its start-time fingerprint intact (pid 1 never counts);
- the run has exactly one ``owner_admitted`` session and ``HERMES_SESSION_ID`` equals it.
"""

from __future__ import annotations

import json
import os
from typing import Mapping, Optional

WORKER_TASK_ENV = "HERMES_KANBAN_WORKER_TASK"


def names_card(task_id: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """True when this process's env says it is a tool child of ``task_id``'s worker (unverified)."""
    env = os.environ if env is None else env
    return (env.get(WORKER_TASK_ENV) or "").strip() == task_id


def _payloads(conn, task_id: str, run_id: int, kind: str) -> list[dict]:
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? AND kind = ? ORDER BY id",
                        (task_id, run_id, kind)).fetchall()
    out = []
    for row in rows:
        try:
            payload = json.loads(row[0]) if row[0] else None
        except (TypeError, ValueError):
            payload = None
        out.append(payload if isinstance(payload, dict) else {})
    return out


def _descends_from(pid) -> bool:
    from hermes_cli.top_level_env import _is_ancestor, _pid_alive
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    return pid > 1 and _pid_alive(pid) and _is_ancestor(pid)


def run_for(conn, task_id: str, env: Optional[Mapping[str, str]] = None) -> tuple[Optional[int], Optional[str]]:
    """``(run_id, None)`` when this process is proven to be ``task_id``'s worker shell; ``(None, why)`` when the env
    names the card but the proof fails; ``(None, None)`` when the env does not name the card at all."""
    env = os.environ if env is None else env
    if not names_card(task_id, env):
        return None, None
    task = conn.execute("SELECT status, claim_lock, current_run_id, worker_pid, worker_started_at FROM tasks "
                        "WHERE id = ?", (task_id,)).fetchone()
    if task is None or task["status"] != "running" or not task["claim_lock"] or task["current_run_id"] is None:
        return None, f"{task_id} has no claimed running run"
    run_id = int(task["current_run_id"])
    run = conn.execute("SELECT status, ended_at FROM task_runs WHERE id = ? AND task_id = ?",
                       (run_id, task_id)).fetchone()
    if run is None or run["ended_at"] is not None or run["status"] != "running":
        return None, f"{task_id}'s current run {run_id} is not open"
    bound = [p.get("pid") for p in _payloads(conn, task_id, run_id, "worker_bound")]
    from hermes_cli.kanban_db_dispatch import _worker_alive
    spawned = task["worker_pid"] if task["worker_pid"] and _worker_alive(task["worker_pid"],
                                                                         task["worker_started_at"]) else None
    if not any(_descends_from(pid) for pid in [*bound, spawned]):
        return None, f"pid {os.getpid()} does not descend from run {run_id}'s live worker"
    sessions = {p.get("session_id") for p in _payloads(conn, task_id, run_id, "owner_admitted")}
    if len(sessions) != 1 or not isinstance(next(iter(sessions)), str) or not next(iter(sessions)):
        return None, f"run {run_id} has no single admitted worker session"
    if (env.get("HERMES_SESSION_ID") or "").strip() != next(iter(sessions)):
        return None, f"HERMES_SESSION_ID is not run {run_id}'s admitted worker session"
    return run_id, None
