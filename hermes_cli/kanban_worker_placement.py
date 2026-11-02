"""Worker-first policy enrollment and execution fences; Board owns assignments."""
from __future__ import annotations

import json
import math
import time

POLICY = "worker-first-v1"
WAITING = "waiting_for_worker_capacity"
MAX_RESERVES_PER_TICK = 4
DISPATCH_NETWORK_BUDGET_S = 2.0


def creation_decision(args, cfg, gateway, pin, origin_intent):
    """Resolve explicit local pins and pool enrollment before legacy routing."""
    from hermes_cli.kanban_placement import PINNED_LOCAL_WHY, portable_reason

    if pin in ("local", "here", (gateway or "").lower()):
        return {"where": "local", "why": PINNED_LOCAL_WHY}
    pools = cfg.get("worker_pools")
    if pin not in ("", "auto") or not isinstance(pools, dict) or gateway not in pools:
        return None
    if args.get("runs_on") is not None:
        return None  # Preserve legacy routing, including an explicit empty pin.
    if origin_intent is not None:
        return None
    reason = portable_reason(args, cfg)
    if reason:
        return None  # Non-portable work still follows the origin's legacy route.
    return {"where": "local", "placement_policy": POLICY, "why": WAITING}


def cli_policy(args, body, workspace_kind, workspace_path, board):
    """CLI creation uses the same enrollment boundary, without changing legacy routes."""
    from hermes_cli import kanban_placement as placement

    cfg = placement.config()
    if not cfg.get("worker_pools"):
        return None
    gateway = placement.reg.self_gateway()
    fields = {**vars(args), "body": body, "workspace_kind": workspace_kind,
              "workspace_path": workspace_path, "board": board}
    if fields.get("initial_status") == "running":
        fields.pop("initial_status")  # argparse's ordinary create default, not a requested hold
    result = creation_decision(fields, cfg, gateway, "auto", None)
    return (result or {}).get("placement_policy")


def validate_creation(policy, *, runs_on, workspace_kind, workspace_path, project, origin_intent, board):
    """Reject contradictory enrollment rather than quietly erasing a caller pin."""
    if policy is None:
        return
    if policy != POLICY:
        raise ValueError("unknown placement policy")
    if (runs_on is not None or workspace_kind not in (None, "scratch") or workspace_path
            or project or origin_intent is not None or board not in (None, "default")):
        raise ValueError("worker-first policy cannot override manual, courier or host placement")


def execution_refusal(conn, row):
    """Local check only; a matching assignment still requires the authoritative lease.

    A persisted policy always fences, including after sync/config rollback. Missing
    or malformed labels cannot turn a queued card into origin-local work.
    """
    from hermes_cli import kanban_db_sync as sync

    row = dict(row)
    policy = row.get("placement_policy")
    if not policy:
        return None
    if policy != POLICY or sync.board_mode(conn) != "primary":
        return WAITING
    state = sync.sync_state(conn) or {}
    try:
        labels = json.loads(row.get("runs_on") or "null")
    except (TypeError, ValueError):
        return WAITING
    own = state.get("gateway_id")
    if not own or labels != [f"machine:{own}"]:
        return WAITING
    return None


def local_refusals(conn, status):
    """Read policy cards even when the registry and sync are unavailable."""
    rows = conn.execute("SELECT * FROM tasks WHERE status=? AND claim_lock IS NULL "
                        "AND placement_policy IS NOT NULL", (status,)).fetchall()
    return {row["id"]: reason for row in rows if (reason := execution_refusal(conn, row))}


def reevaluate(conn, hub, board, gateway, *, cfg=None):
    """Reserve acknowledged origin-owned ready work, then read authoritative feed.

    No lease (even apparently expired) is moved here. A lost reply is retried with
    the same revision; a successful reply is never itself permission to execute.
    """
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_placement as placement
    from hermes_cli import kanban_sync_client as client

    if board != "default" or sync.board_mode(conn) != "primary":
        return 0
    cfg = placement.config() if cfg is None else cfg
    pools = cfg.get("worker_pools")
    candidates = pools.get(gateway) if isinstance(pools, dict) else None
    if (not isinstance(candidates, list) or not candidates or len(candidates) > 64
            or not all(isinstance(name, str) and name for name in candidates)):
        return 0
    cursor = (sync.sync_state(conn) or {}).get("worker_reserve_cursor") or ""
    rows = conn.execute(
        "SELECT id, sync_version FROM tasks WHERE placement_policy=? AND status='ready' "
        "AND created_on=? AND claim_lock IS NULL AND lease_holder IS NULL AND sync_version IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM kanban_sync_outbox o WHERE o.table_name='tasks' "
        "AND o.row_uid=tasks.id AND o.state='pending') ORDER BY (id <= ?), id LIMIT ?",
        (POLICY, gateway, cursor, MAX_RESERVES_PER_TICK)).fetchall()
    attempted = 0
    last = None
    try:
        for row in rows:
            last = row["id"]
            try:
                hub.reserve(board, task_id=row["id"], expected_revision=row["sync_version"], candidates=candidates)
                attempted += 1
            except OSError:
                # An unavailable transport applies to the entire batch, not just this card.
                return attempted
            except client.HubError:
                # Refused revisions still advance the local cursor, but never the feed.
                continue
    finally:
        if last is not None:
            from hermes_cli.kanban_db_connect import write_txn
            with write_txn(conn):
                conn.execute("UPDATE kanban_sync_state SET worker_reserve_cursor=? WHERE board_id=? AND gateway_id=?",
                             (last, board, gateway))
    if attempted:
        client.pull(conn, hub, board, observers=True, partition=gateway)
    return attempted


def acknowledged_outbox(row, task):
    """Policy enrollment is create-only; never replay an old automatic assignment."""
    if task is None or task["sync_version"] is None:
        return row
    fields = row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"])
    fields = dict(fields)
    if task["placement_policy"] or fields.get("placement_policy"):
        fields.pop("runs_on", None)
    fields.pop("placement_policy", None)
    return {**row, "fields": fields}


def local_admission(conn):
    """Fresh local pressure and whole-host occupancy before converting a grant to a run."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as node
    from hermes_cli import machine_metrics

    own = (sync.sync_state(conn) or {}).get("gateway_id")
    try:
        entry = ((node.registry() or {}).get("machines") or {}).get(own) or {}
        capacity = entry.get("max_card_runs")
        sample = machine_metrics.collect()
        load, cpus, memory, active = (sample.get(k) for k in ("cpu_load1", "cpus", "mem_used_pct", "active_card_runs"))
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    if not all(type(v) in (int, float) and math.isfinite(v) for v in (load, cpus, memory)):
        return False
    if type(capacity) is not int or capacity <= 0 or type(active) is not int or active < 0:
        return False
    return (cpus > 0 and load >= 0 and 0 <= memory <= 100 and active < capacity
            and max(load / cpus, memory / 100) < 0.85)


def dispatch_tick(conn):
    """Retry placement before local capacity checks, without creating an extra daemon."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client

    if sync.board_mode(conn) != "primary":
        return
    state = sync.sync_state(conn) or {}
    if not conn.execute("SELECT 1 FROM tasks WHERE placement_policy=? AND status='ready' LIMIT 1", (POLICY,)).fetchone():
        return
    try:
        ident = client.load_identity()
        if ident.gateway != state.get("gateway_id"):
            return
        hub = client.Hub(ident, timeout=DISPATCH_NETWORK_BUDGET_S,
                         deadline=time.monotonic() + DISPATCH_NETWORK_BUDGET_S)
        client.pull(conn, hub, state["board_id"], observers=True, partition=ident.gateway)
        reevaluate(conn, hub, state["board_id"], ident.gateway)
    except (client.HubError, client.SyncUnavailable, client.Resync, OSError):
        # Offline origin cannot remove the queue fence or manufacture an assignment.
        return

