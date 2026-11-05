"""Bounded optional Desktop owner read when a local task replica is absent."""
from __future__ import annotations

import inspect
import json
import time

from hermes_cli.kanban_wait_replica import STATUSES, observe_wait


def owner_proof(conn, task):
    """Owner-produced evidence; the relay must not infer it from summary prose."""
    from hermes_cli.kanban_db_connect import _main_db_file
    from hermes_cli.kanban_db_sync import board_slug_for_path
    board = board_slug_for_path(_main_db_file(conn))
    return {"board": board, "human_request": bool(task.human_request),
            **observe_wait(conn, task.id, now=int(time.time()))}


def read_task(gateway, target, *, board=None, timeout_seconds=8):
    """Only a qualified, explicitly live response with owner wait evidence is usable."""
    try:
        from hermes_cli.plugins import discover_plugins
        from tools.registry import registry
        if timeout_seconds <= 0:
            raise ValueError("live task lookup budget exhausted")
        discover_plugins()
        entry = registry.get_entry("cross_task")
        # Older handlers ignore unknown kwargs and can wait 150s. Never invoke
        # one under the scheduler's 30s probe budget.
        if entry is None or "timeout_seconds" not in inspect.signature(entry.handler).parameters:
            raise ValueError("bounded live task transport unavailable; update session-mentions")
        board = board or "default"
        raw = entry.handler({"action": "read", "target": f"@task:{board}@{gateway}/{target}", "limit": 1},
                            timeout_seconds=min(8, timeout_seconds))
        data = json.loads(raw)
        if data.get("ok") is not True:
            raise ValueError(str(data.get("error") or "live task lookup failed")[:200])
        task = data.get("task") or {}
        resolved_gateway = data.get("gateway")
        proof = task.get("wait_observation") or {}
        if (not isinstance(resolved_gateway, str) or not resolved_gateway
                or data.get("ref") != f"@task:{board}@{resolved_gateway}/{target}"
                or data.get("board") != board or proof.get("board") != board
                or task.get("source") != "live" or task.get("id") != target
                or task.get("status") not in STATUSES
                or type(proof.get("parked_agent")) is not bool
                or type(proof.get("human_request")) is not bool
                or "orphan_reason" not in proof):
            raise ValueError("live task transport returned no matching owner observation")
        task["human_request"] = proof["human_request"]
        return task
    except Exception as exc:  # health: allow BLE001 -- probe exceptions must be rc2, never process-exit 1 (pending)
        raise ValueError(f"live task transport: {exc}") from exc
