"""Worker-first policy enrollment and execution fences; Board owns assignments."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time

logger = logging.getLogger(__name__)

POLICY = "worker-first-v1"
WAITING = "waiting_for_worker_capacity"
MAX_RESERVES_PER_TICK = 4
DISPATCH_NETWORK_BUDGET_S = 2.0
# A 409 not_claimable means the Board row moved on (leased, or not ready) and this copy has not pulled it yet.
# Re-asking at the same revision every dispatcher tick only repeats the refusal; wait for a new revision instead.
NOT_CLAIMABLE_BACKOFF_S = 600.0
_refused: dict[tuple[str, str], tuple[int, float]] = {}  # (board, task) -> (refused revision, retry after)
# No assigned keepalive exists here. Sixty seconds bounds queue probes while still discovering
# registry-only capacity changes without waiting for a task revision. Assigned renewals are unchanged.
RESERVE_KEEPALIVE_S = 60.0
# (board, task) -> (queued revision, local notice revision, retry after). The notice revision (required labels, run
# count) is part of the identity: a requirement change the Board would answer differently is never throttled away.
_queued: dict[tuple[str, str], tuple[int, str | None, float]] = {}


def _local_identity(conn, task_id):
    from hermes_cli import kanban_repin_notice as repin

    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return "" if row is None else json.dumps(repin.notice_revision(conn, task_id, row), sort_keys=True)


def _notice_from_stored(conn, task_id, gateway, pool):
    """A throttled pass makes no Board request, but the owner's repin notice still runs from the stored current
    observation (local only, once per card revision), so a card queued before its notice existed still gets it."""
    from hermes_cli import kanban_queue_reason as queue
    from hermes_cli import kanban_repin_notice as repin

    # Only reached while the identity is unchanged, and maybe_notify re-fences ownership and lifecycle itself.
    repin.maybe_notify(conn, task_id, gateway, queue.latest(conn, task_id), pool)


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


def creation_audit(args, cfg=None):
    """``{"portable_reason": <why it cannot leave its origin> | None}`` for the card's ``created`` event, or None when
    no worker pool is configured (nothing to audit). Records the structural check every create runs, so the share of
    cards a pool never sees, and why, is a query instead of a replay (kanban-12ee0e4b65). Never raises. [daveey]"""
    from hermes_cli import kanban_placement as placement

    try:
        cfg = placement.config() if cfg is None else cfg
        if not isinstance(cfg, dict) or not cfg.get("worker_pools"):
            return None
        return {"portable_reason": placement.portable_reason(args, cfg)}
    except Exception:  # health: allow BLE001 -- an audit field never blocks a create
        logger.warning("portable_reason audit failed", exc_info=True)
        return None


def resolved_db_policy(fields):
    """Validate the effective workspace after project inheritance, enrolling only unchecked producers."""
    policy = fields["placement_policy"]
    if policy is None and not fields["placement_checked"]:
        policy = db_policy(fields)
    validate_creation(policy, runs_on=fields["runs_on"], workspace_kind=fields["workspace_kind"],
                      workspace_path=fields["workspace_path"], project=fields["project_id"],
                      origin_intent=None, board=fields["board"])
    return policy


def db_policy(fields):
    """Enroll direct producers at insertion; front doors may preserve an already checked decision."""
    from hermes_cli import kanban_placement as placement
    from hermes_cli.kanban_db_connect import _main_db_file
    from hermes_cli.kanban_db_sync import board_slug_for_path

    cfg = placement.config()
    if not cfg.get("worker_pools"):
        return None
    args = {**fields, "project": fields.get("project_id")}
    db_file = _main_db_file(fields["conn"])
    args["board"] = fields.get("board") or (board_slug_for_path(db_file) if db_file else "default")
    if args.get("initial_status") == "running":
        args.pop("initial_status")
    result = creation_decision(args, cfg, placement.reg.self_gateway(), "auto", None)
    return (result or {}).get("placement_policy")


def _cli_fields(args, body, workspace_kind, workspace_path, board):
    fields = {**vars(args), "body": body, "workspace_kind": workspace_kind,
              "workspace_path": workspace_path, "board": board}
    if fields.get("initial_status") == "running":
        fields.pop("initial_status")  # argparse's ordinary create default, not a requested hold
    return fields


def cli_policy(args, body, workspace_kind, workspace_path, board):
    """CLI creation uses the same enrollment boundary, without changing legacy routes."""
    from hermes_cli import kanban_placement as placement

    cfg = placement.config()
    if not cfg.get("worker_pools"):
        return None
    gateway = placement.reg.self_gateway()
    result = creation_decision(_cli_fields(args, body, workspace_kind, workspace_path, board), cfg, gateway,
                               "auto", None)
    return (result or {}).get("placement_policy")


def cli_audit(args, body, workspace_kind, workspace_path, board):
    """:func:`creation_audit` over the same fields :func:`cli_policy` judged."""
    return creation_audit(_cli_fields(args, body, workspace_kind, workspace_path, board))


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
    from hermes_cli import kanban_queue_reason as queue

    attempted = 0
    last = None
    pulled = []  # at most one feed pull per tick for not_claimable pickups (shared network budget)
    # Every reserve below is made from this pre-loop copy, so a refusal is judged against it even after an in-loop
    # pull moved a later row on: the refused row (fence) and the reservation it was waiting on.
    before = {r["id"]: (queue.snapshot(conn, r["id"]), _reserved_machine(queue, conn, r["id"])) for r in rows}
    now = time.monotonic()
    try:
        for row in rows:
            last = row["id"]
            key = (board, row["id"])
            held = _refused.get(key)
            if held and held[0] == row["sync_version"] and now < held[1]:
                continue  # refused not_claimable at this revision: wait for the feed, don't re-ask every tick
            _refused.pop(key, None)
            if len(_refused) > 1024:  # cards that left the query never come back to clear their entry
                for stale in [k for k, (_, until) in _refused.items() if until <= now]:
                    del _refused[stale]
            labels = requested_move(conn, row["id"])
            stage = "reserve" if labels is None else "repin"  # provenance for a failure below
            held = _queued.get(key)
            identity = _local_identity(conn, row["id"]) if labels is None else None
            if labels is None and held and held[:2] == (row["sync_version"], identity) and now < held[2]:
                queue.try_record(_notice_from_stored, conn, row["id"], gateway, candidates)
                continue
            _queued.pop(key, None)
            started = time.monotonic()  # timing provenance for a failure below: budget left at call start
            budget = queue.call_budget(hub, started)
            try:
                if labels is not None:
                    # A run moved this card (kanban_move). Its runs_on is Board-owned, so only the origin's
                    # creator repin can apply that move; reserving would hand it straight back to the pool.
                    reply = hub.repin(board, task_id=row["id"], expected_revision=row["sync_version"], runs_on=labels)
                    queue.clear_streak(gateway, row["id"])
                    try:
                        _record_repin(conn, row["id"], gateway, labels, reply)
                    except sqlite3.Error:
                        logger.warning("repinned %s on the Board but could not record it locally", row["id"],
                                       exc_info=True)
                else:
                    reply = hub.reserve(board, task_id=row["id"], expected_revision=row["sync_version"],
                                        candidates=candidates)
                    if reply.get("state") == "queued":
                        # Never adopt an arbitrary post-pull revision: it may be somebody else's edit.
                        version = reply.get("task_version", row["sync_version"])
                        _queued[key] = (version, identity, time.monotonic() + RESERVE_KEEPALIVE_S)
                        if len(_queued) > 1024:
                            del _queued[next(iter(_queued))]
                    queue.try_record(queue.record_reply, conn, row["id"], gateway, reply, candidates)
                attempted += 1
            except OSError as exc:
                timing = queue.call_timing(budget, started, time.monotonic())
                # An unavailable transport applies to the entire batch, not just this card.
                queue.try_record(queue.record_error, conn, row["id"], gateway, error=exc, stage=stage, timing=timing)
                return attempted
            except client.HubError as exc:
                timing = queue.call_timing(budget, started, time.monotonic())
                fence = None
                if rejection_code(exc) == "not_claimable":
                    # The Board already has it leased or not ready: our copy is behind. Back off at this revision.
                    _refused[key] = (row["sync_version"], now + NOT_CLAIMABLE_BACKOFF_S)
                    fence, machine = before[row["id"]]
                    if picked_up(conn, hub, board, gateway, row["id"], machine, pulled):
                        queue.clear_streak(gateway, row["id"])
                        continue  # the worker we reserved already claimed it: a pickup, not a refusal
                queue.try_record(queue.record_error, conn, row["id"], gateway, error=exc, fence=fence, stage=stage,
                                 timing=timing)
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


def _reserved_machine(queue, conn, task_id):
    """The machine of the current assignment observation (the Board's reserve reply), or None."""
    value = queue.latest(conn, task_id) or {}
    return value.get("machine") if value.get("state") == "assigned" and value.get("current") else None


def picked_up(conn, hub, board, gateway, task_id, machine, pulled):
    """Whether a ``not_claimable`` reserve only lost the race to ``machine``, the worker this origin reserved.

    The Board leases the card to its reserved worker and drops the reservation in one write, so a reserve made from
    a copy that has not pulled that lease is refused. Pull the feed (once per tick: ``pulled`` remembers it) and
    look for the lease, or a Board ``claimed`` event newer than the reservation, held by the reserved machine.
    Anything else (no current reservation, a failed pull, another holder) stays a refusal.
    """
    from hermes_cli import kanban_queue_reason as queue
    from hermes_cli import kanban_sync_client as client

    if not machine:
        return False
    if not pulled:
        pulled.append(True)
        try:
            client.pull(conn, hub, board, observers=True, partition=gateway)
        except Exception as exc:  # health: allow BLE001 -- a failed pull keeps the refusal; never abort the batch
            logger.warning("feed pull after not_claimable failed (%s)", type(exc).__name__)
            return False
    row = conn.execute("SELECT lease_holder FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is not None and row["lease_holder"] == machine:
        return True
    since = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id=? AND kind=?",
                         (task_id, queue.EVENT)).fetchone()[0]
    claim = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='claimed' AND id>? AND json_valid(payload) "
        "AND json_extract(payload, '$.holder') IS NOT NULL ORDER BY id DESC LIMIT 1", (task_id, since)).fetchone()
    return bool(claim and json.loads(claim[0]).get("holder") == machine)


def rejection_code(exc):
    """The Board's rejection code from a HubError body (``{"error": "<code>"}``), or None."""
    try:
        body = json.loads(exc.detail)
    except (TypeError, ValueError):
        return None
    code = body.get("error") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


def requested_move(conn, task_id):
    """Labels of the latest unapplied ``kanban_move`` of a policy card, or None.

    A pool worker's run cannot change a worker-first card's ``runs_on`` (Board-owned: the outbox strips it and the
    Board fences the edit), so its ``moved`` event is the request; the origin applies it with the creator repin,
    which writes a ``moved`` event flagged ``repinned``. The latest event decides, so an applied move never replays.
    """
    from hermes_cli import kanban_move as km
    from hermes_cli import kanban_worker_node as node

    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? AND json_valid(payload) "
        "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id, km.MOVED_EVENT)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
        if not isinstance(payload, dict) or payload.get("repinned"):
            return None
        labels = node.normalize_runs_on(payload.get("runs_on"))
    except (TypeError, ValueError):
        return None
    return labels or None


def _record_repin(conn, task_id, gateway, labels, reply):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_move as km
    from hermes_cli.kanban_worker_repin import REPINNED_EVENT_FLAG

    version = reply.get("version") if isinstance(reply, dict) else None
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, km.MOVED_EVENT, {
            "from_machine": gateway, "runs_on": labels, "reason": "applied the run's kanban_move as the creator repin",
            "queued": True, REPINNED_EVENT_FLAG: True, "board_version": version})


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
        from hermes_cli import kanban_queue_reason as queue
        try:
            client.pull(conn, hub, state["board_id"], observers=True, partition=ident.gateway)
        except (client.HubError, client.SyncUnavailable, client.Resync, OSError) as exc:
            queue.try_record(queue.record_pull_error, conn, ident.gateway, error=exc)
            return
        queue.clear_pull_streaks(ident.gateway)  # a feed-pull failure streak is no longer consecutive
        reevaluate(conn, hub, state["board_id"], ident.gateway)
    except (client.HubError, client.SyncUnavailable, client.Resync, OSError):
        # Offline origin cannot remove the queue fence or manufacture an assignment.
        return

