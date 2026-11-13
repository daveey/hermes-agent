"""Worker-first policy enrollment and execution fences; Board owns assignments."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from contextlib import ExitStack

logger = logging.getLogger(__name__)

POLICY = "worker-first-v1"
WAITING = "waiting_for_worker_capacity"
MAX_RESERVES_PER_TICK = 4
DISPATCH_NETWORK_BUDGET_S = 2.0
# A 409 not_claimable means the Board row moved on (leased, or not ready) and this copy has not pulled it yet.
# Re-asking at the same revision every dispatcher tick only repeats the refusal; wait for a new revision instead.
NOT_CLAIMABLE_BACKOFF_S = 600.0
_refused: dict[tuple[str, str], tuple[int, float]] = {}  # (board, task) -> (refused revision, retry after)
# (board, task) -> revision at which the Board refused the worker-first self-check's repin: any 4xx but
# 408/425/429, coded or not, and a 501; except not_claimable, which _refused covers. Other 5xx and 408/425/429 are
# outages, retried next tick. While the card stays at that revision it goes back to the reserve rather
# than re-asking a repin the Board already refused; a new revision asks again. Explicit kanban_move repins never
# fall back: a reserve would hand the run's move straight back to the pool.
_selfcheck_refused: dict[tuple[str, str], int] = {}
# Same rule for the pickup gate's origin repin (machine:<origin>), keyed apart: a refused pool repin must still let
# the origin repin be asked once at that revision (review of PR #2362, t_01a1d5edeefa8430).
_origin_refused: dict[tuple[str, str], int] = {}
_RETRYABLE_4XX = frozenset({408, 425, 429})  # request timeout, too early, rate limited: not the Board's answer
# No assigned keepalive exists here. Sixty seconds bounds queue probes while still discovering
# registry-only capacity changes without waiting for a task revision. Assigned renewals are unchanged.
RESERVE_KEEPALIVE_S = 60.0
# (board, task) -> (queued revision, local notice revision, retry after). The notice revision (required labels, run
# count) is part of the identity: a requirement change the Board would answer differently is never throttled away.
_queued: dict[tuple[str, str], tuple[int, str | None, float]] = {}


def _remember_queued(reply, key, revision, identity):
    if reply.get("state") == "queued":
        # Never adopt an arbitrary post-pull revision: it may be somebody else's edit.
        version = reply.get("task_version", revision)
        _queued[key] = (version, identity, time.monotonic() + RESERVE_KEEPALIVE_S)
        if len(_queued) > 1024:
            del _queued[next(iter(_queued))]


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


QUEUED_WHY = ("portable: queued for any machine labelled {label} (kanban.placement.worker_queue); the first one "
              "with a free slot claims it through the Board lease")
UNMET_WHY = " (local marker {hit} not met on {gateway})"
TAILNET_LABEL = "capability:tailnet-exec"
# Labels a built-in local-marker group adds on a pool worker (kanban_placement.LOCAL_MARKER_GROUPS).
UNMET_GROUP_LABELS = {"mac": (TAILNET_LABEL,), "desktop": (TAILNET_LABEL,)}
_MAC_NOTE = ("{hit}: no Board machine this card can lease runs macOS (the Macs take no Board leases); it joins the "
             "pool with capability:tailnet-exec, so a run that needs the Mac reaches it with tailnet_exec/ssh, and a "
             "GUI-only step (a TCC or Keychain prompt, a signing UI) is a human_request.")
UNMET_GROUP_NOTES = {
    "mac": _MAC_NOTE, "desktop": _MAC_NOTE,
    "browser": ("{hit}: pool workers have a headless browser without sign-in; work that needs the hub's signed-in "
                "Chrome must be created with runs_on=['machine:hub'] and hub_reason (admission-judged)."),
    "sso": "{hit}: credentials come from token-broker; an SSO login only David can do is a human_request.",
    "context": "{hit}: a context skill every pool worker carries.",
}


def _hit_name(why):
    return why.removeprefix("mentions ")


def unmet_markers(args, cfg):
    """``{labels, note, hit}`` for the card's built-in local markers, which a pool worker cannot meet better than
    the pool; None when it has none or any hit is an operator pattern (t_01a1d2662553185d). The gateway test and
    every other keep are the caller's (:func:`hermes_cli.kanban_home_marker_placement.pool_worker_markers`).
    [daveey]"""
    from hermes_cli import kanban_placement as placement

    groups = placement.builtin_marker_groups(args, cfg)
    if not groups:
        return None
    labels, notes = set(), []
    for group, why in groups.items():
        labels.update(UNMET_GROUP_LABELS.get(group, ()))
        note = UNMET_GROUP_NOTES[group].format(hit=_hit_name(why))
        if note not in notes and not (group == "desktop" and "mac" in groups):
            notes.append(note)
    first = next(iter(groups.values()))
    return {"labels": labels, "note": " ".join(notes), "hit": _hit_name(first)}


def queue_decision(args, cfg, gateway, pin, machine_ok=None):
    """``runs_on: [<pool label>, *coordination labels]`` for a portable card of a ``worker_queue`` gateway, or None.

    One queue instead of a machine pin: no worker is chosen at creation, so a held or full worker never strands the
    card while another has free slots. None (legacy routing applies) for a pin, an explicit ``runs_on``,
    non-portable work, or when no active, unheld machine carries the label. Courier and origin-bound creates queue
    too: chat ownership pins storage, not execution (t_01a1d0571c2f07d6). On a pool worker a built-in local-marker
    hit, and a home-gateway-marker hit this worker holds no state for, queue too, with their labels and a ``note``
    (:func:`hermes_cli.kanban_home_marker_placement.pool_worker_markers`). Either way the card queues only when one
    active, unheld machine holds the whole label set (queue, marker and coordination labels), judged by
    ``machine_ok`` (given the label tuple) or the registry; else the local keep stands. The registry counts only
    machines seen within ``FRESH_S`` (:func:`kanban_placement.label_machines`), so during a Board outage the card
    stays local once the last good snapshot ages out, instead of queueing on ``pool:worker``. A home-marker keep
    there is returned as ``{"where": "local", "why": "home gateway marker: ...", "note": ...}``. [daveey]
    """
    from hermes_cli import kanban_placement as placement
    from hermes_cli import kanban_placement_coordination as coordination

    label = placement._Cfg(cfg).queue_label(gateway)
    if not label or pin not in ("", "auto") or args.get("runs_on") is not None:
        return None
    unmet = None
    reason = placement.portable_reason(args, cfg)
    if reason in ("local marker", "home gateway marker"):
        from hermes_cli.kanban_home_marker_placement import pool_worker_markers
        # An injected machine_ok seam is the one liveness source and judges the whole label set below; only the
        # registry path checks the bare queue label first (the gateway test).
        unmet = pool_worker_markers(args, cfg, gateway, reason,
                                    lambda: machine_ok is not None or placement._machine_picks_up(label))
        if unmet is None:
            return None
        if unmet.get("keep"):
            return unmet["keep"]
    elif reason:
        return None
    extra = coordination.required_labels(args, cfg)
    if extra is None:
        return None
    if unmet is not None:
        extra = extra | unmet["labels"]
    runs_on = [label, *sorted(extra - {label})]
    # One liveness source for the whole set, marker hit or not: an active, unheld pickup machine must hold every
    # label, else nobody could claim the card and today's local keep stands. The machine_ok seam sees the set.
    if not (machine_ok or placement._machine_picks_up)(label if len(runs_on) == 1 else tuple(runs_on)):
        if unmet is not None:
            from hermes_cli.kanban_home_marker_placement import unclaimable
            unclaimable(args)  # a home-marker queue record (if any) becomes gateway-test-failed
        return None
    if unmet is None:
        return {"where": "local", "runs_on": runs_on, "why": QUEUED_WHY.format(label=label)}
    return {"where": "local", "runs_on": runs_on, "note": unmet["note"],
            "why": QUEUED_WHY.format(label=label) + unmet["why"]}


def creation_decision(args, cfg, gateway, pin, origin_intent, machine_ok=None, queue=True):
    """Resolve explicit local pins, the worker queue and pool enrollment before legacy routing."""
    from hermes_cli.kanban_placement import PINNED_LOCAL_WHY, portable_reason

    if pin in ("local", "here", (gateway or "").lower()):
        return {"where": "local", "why": PINNED_LOCAL_WHY}
    queued = queue_decision(args, cfg, gateway, pin, machine_ok) if queue else None
    if queued is not None:
        return queued
    from hermes_cli import kanban_placement as placement
    from hermes_cli import kanban_placement_coordination as coordination
    if placement._Cfg(cfg).queue_label(gateway):
        # On a worker_queue gateway this decision never falls back to worker-first: a queue miss (no live labelled
        # machine, a non-primary board) stays home under legacy routing (the hub's admission judge may still route a
        # card it denies worker-first via pool_route). The fallback reserved one machine at a time from a
        # stale worker_pools list (MacBook t_01a1d5386cb55abf: worker-2..4 only, 140 reservations). [daveey]
        return None
    pools = cfg.get("worker_pools")
    if pin not in ("", "auto") or not isinstance(pools, dict) or gateway not in pools:
        return None
    if coordination.required_labels(args, cfg) is None:
        return None  # claim_refusal refuses this text on every pool worker: enrolled, it could never run
    if args.get("runs_on") is not None:
        return None  # Preserve legacy routing, including an explicit empty pin.
    if origin_intent is not None:
        return None
    reason = portable_reason(args, cfg)
    if reason:
        return None  # Non-portable work still follows the origin's legacy route.
    from hermes_cli.kanban_mandatory_pin import worker_first_refusal
    if worker_first_refusal(args):
        return None  # the worker-first lease cannot demand the pin attestation: home, as before
    return {"where": "local", "placement_policy": POLICY, "why": WAITING}


def creation_audit(args, cfg=None):
    """Record structural portability for every create, independent of pool enrollment.

    None means structurally portable, not admitted. Use the caller's placement config even without worker_pools;
    its assignee allowlist and host markers still apply. A failed audit is explicit, never portable. [daveey]
    """
    from hermes_cli import kanban_placement as placement

    explicit = args.get("placement") not in (None, "", "auto") or args.get("runs_on") is not None
    intent = "explicit" if explicit else "auto"
    try:
        cfg = placement.config() if cfg is None else cfg
        audit = {"portable_reason": placement.portable_reason(args, cfg), "placement_intent": intent}
        from hermes_cli.kanban_home_marker_placement import recorded
        if (home := recorded(args)) is not None:  # how a pool worker resolved its home-marker hits
            audit["home_marker"] = home
        return audit
    except Exception:  # health: allow BLE001 -- an audit field never blocks a create
        logger.warning("portable_reason audit failed", exc_info=True)
        return {"portable_reason": "audit failed", "placement_intent": intent}


def resolved_db_placement(fields):
    """``(policy, audit, runs_on)``: validate the effective workspace after project inheritance, placing only
    unchecked producers (worker queue, else pool enrollment) exactly as the front doors do."""
    policy, runs_on = fields["placement_policy"], fields["runs_on"]
    if policy is None and not fields["placement_checked"]:
        policy, queued = db_placement(fields)
        runs_on = queued if queued is not None else runs_on
    validate_creation(policy, runs_on=runs_on, workspace_kind=fields["workspace_kind"],
                      workspace_path=fields["workspace_path"], project=fields["project_id"],
                      origin_intent=None, board=fields["board"])
    audit = fields["placement_audit"]
    if audit is None:
        audit = creation_audit(_db_fields(fields))
    return policy, audit, runs_on


def db_placement(fields):
    """``(placement_policy, runs_on)`` for a direct ``create_task`` producer (swarm, triage sweep, alerts, ...).

    Same outcome as the front doors: a portable card of a ``worker_queue`` gateway queues for the pool label on a
    Board primary (``runs_on`` is a Board placement field); a queue miss there, or off a primary, stays home
    (:func:`creation_decision` itself never falls back to worker-first on a queue gateway; on the hub a queue miss
    goes to the admission judge, and a card it denies can still get worker-first from
    :func:`kanban_hub_admission_placement.pool_route`). Only a gateway without a
    ``worker_queue`` keeps worker-pool enrollment. Judging the queue but keeping only ``placement_policy`` (11-12)
    dropped both. [daveey]
    """
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_placement as placement

    cfg = placement.config()
    if not cfg.get("worker_pools") and not cfg.get("worker_queue"):
        return None, None
    queue = bool(cfg.get("worker_queue")) and sync.board_mode(fields["conn"]) == "primary"
    result = creation_decision(_db_fields(fields), cfg, placement.reg.self_gateway(), "auto", None,
                               queue=queue) or {}
    if result.get("runs_on") is not None:
        return None, list(result["runs_on"])
    return result.get("placement_policy"), None


def _db_fields(fields):
    """Normalize direct-DB options exactly as front doors do, using the actual connection's board."""
    from hermes_cli.kanban_db_connect import _main_db_file
    from hermes_cli.kanban_db_sync import board_slug_for_path

    args = {**fields, "project": fields.get("project_id")}
    db_file = _main_db_file(fields["conn"])
    args["board"] = fields.get("board") or (board_slug_for_path(db_file) if db_file else "default")
    if args.get("initial_status") == "running":
        args.pop("initial_status")
    return args


def _cli_fields(args, body, workspace_kind, workspace_path, board, *, assignee):
    fields = {**vars(args), "body": body, "workspace_kind": workspace_kind,
              "workspace_path": workspace_path, "board": board, "assignee": assignee}
    if fields.get("initial_status") == "running":
        fields.pop("initial_status")  # argparse's ordinary create default, not a requested hold
    return fields


def cli_policy(args, body, workspace_kind, workspace_path, board, *, assignee):
    """CLI creation uses the same enrollment boundary, without changing legacy routes."""
    from hermes_cli import kanban_placement as placement

    cfg = placement.config()
    if not cfg.get("worker_pools"):
        return None
    gateway = placement.reg.self_gateway()
    result = creation_decision(_cli_fields(args, body, workspace_kind, workspace_path, board, assignee=assignee), cfg, gateway,
                               "auto", None)
    return (result or {}).get("placement_policy")


def cli_runs_on(conn, args, body, workspace_kind, workspace_path, board, *, assignee):
    """``runs_on`` for a CLI-created card: an explicit ``--runs-on`` as given, else what the worker queue
    (``kanban.placement.worker_queue``) gives it, or None. The queue applies only on a Board primary
    (``runs_on`` is a Board placement field)."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_placement as placement

    if getattr(args, "runs_on", None) is not None:
        return args.runs_on
    if sync.board_mode(conn) != "primary":
        return None
    cfg = placement.config()
    if not cfg.get("worker_queue"):
        return None
    fields = _cli_fields(args, body, workspace_kind, workspace_path, board, assignee=assignee)
    fields.pop("runs_on", None)
    result = queue_decision(fields, cfg, placement.reg.self_gateway(), "auto")
    return (result or {}).get("runs_on")


def cli_audit(args, body, workspace_kind, workspace_path, board, *, assignee):
    """:func:`creation_audit` over the same fields :func:`cli_policy` judged."""
    return creation_audit(_cli_fields(args, body, workspace_kind, workspace_path, board, assignee=assignee))


def validate_creation(policy, *, runs_on, workspace_kind, workspace_path, project, origin_intent, board):
    """Reject contradictory enrollment rather than quietly erasing a caller pin.

    A courier create may carry the policy: only the receiving hub's admission judge sets it
    (``courier_plan``), re-enrolling a denied routed card in the worker pool. [daveey]
    """
    del origin_intent
    if policy is None:
        return
    if policy != POLICY:
        raise ValueError("unknown placement policy")
    if (runs_on is not None or workspace_kind not in (None, "scratch") or workspace_path
            or project or board not in (None, "default")):
        raise ValueError("worker-first policy cannot override manual or host placement")


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
    from hermes_cli.kanban_placement_coordination import claim_refusal
    return claim_refusal(row, own)


def local_refusals(conn, status):
    """Read policy cards even when the registry and sync are unavailable."""
    rows = conn.execute("SELECT * FROM tasks WHERE status=? AND claim_lock IS NULL "
                        "AND placement_policy IS NOT NULL", (status,)).fetchall()
    return {row["id"]: reason for row in rows if (reason := execution_refusal(conn, row))}


def reevaluate(conn, hub, board, gateway, *, cfg=None, may_pull=True):
    """Reserve acknowledged origin-owned ready work, then read authoritative feed.

    No lease (even apparently expired) is moved here. A lost reply is retried with
    the same revision; a successful reply is never itself permission to execute.
    ``may_pull=False`` (a sync round holds the board and pulls this file) reads no feed here.
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
    from hermes_cli import kanban_placement_recovery as recovery
    rows = recovery.select_rows(conn, board, gateway, cursor, MAX_RESERVES_PER_TICK)
    from hermes_cli import kanban_queue_reason as queue
    from hermes_cli import kanban_worker_pickup_gate as pickup_gate

    attempted = 0
    last = None
    pulled = [] if may_pull else [True]  # at most one feed pull per tick for not_claimable pickups (shared budget)
    # Every reserve below is made from this pre-loop copy, so a refusal is judged against it even after an in-loop
    # pull moved a later row on: the refused row (fence) and the reservation it was waiting on.
    before = {r["id"]: (queue.snapshot(conn, r["id"]), _reserved_machine(queue, conn, r["id"])) for r in rows}
    now = time.monotonic()
    try:
        for row in rows:
            if row["placement_policy"] is not None:
                last = row["id"]
            mode = recovery.available_mode(conn, dict(row), cfg, hub, board, gateway)
            # Recovery changes placement policy: revalidate its current inputs. Ordinary
            # renewals retain the pre-loop snapshot used by picked_up after an in-loop pull.
            if mode and not recovery.unchanged_ready(conn, row):
                continue
            if row["placement_policy"] is None and mode is None:
                continue
            key = (board, row["id"])
            if _backed_off(key, row["sync_version"], now):
                continue  # refused not_claimable at this revision: wait for the feed, don't re-ask every tick
            labels, why, skip, refusals = _repin_target(conn, row, key, gateway, cfg, mode, candidates)
            if skip:
                continue  # no candidate's pickup gate takes it and no repin may be asked: never reserve
            stage = "recover" if mode else ("reserve" if labels is None else "repin")
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
                        _record_repin(conn, row["id"], gateway, labels, reply, reason=why)
                    except sqlite3.Error:
                        logger.warning("repinned %s on the Board but could not record it locally", row["id"],
                                       exc_info=True)
                else:
                    from hermes_cli.kanban_placement_recovery import request_assignment
                    reply = request_assignment(hub, board, row, mode, candidates)
                    _remember_queued(reply, key, row["sync_version"], identity)
                    queue.try_record(queue.record_reply, conn, row["id"], gateway, reply, candidates,
                                     refused=pickup_gate.refusals(row, candidates))
                attempted += 1
            except OSError as exc:
                timing = queue.call_timing(budget, started, time.monotonic())
                # An unavailable transport applies to the entire batch, not just this card.
                queue.try_record(queue.record_error, conn, row["id"], gateway, error=exc, stage=stage, timing=timing)
                return attempted
            except client.HubError as exc:
                timing = queue.call_timing(budget, started, time.monotonic())
                if recovery.unavailable_endpoint(hub, board, gateway, exc, mode):
                    logger.info("Board recovery endpoint unavailable; backing off for %ss", recovery.RECOVERY_RETRY_S)
                    continue
                fence = None
                _remember_refusal(refusals, key, row["sync_version"], exc)
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
    if attempted and may_pull:
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


def _move_target(conn, row, gateway, cfg):
    """``(labels, self-check reason)``: the run's unapplied ``kanban_move`` (reason None), else the worker-first
    self-check's pool target for a card stranded with no ``runs_on``, else ``(None, None)`` (reserve)."""
    labels = requested_move(conn, row["id"])
    if labels is not None:
        return labels, None
    from hermes_cli.kanban_worker_first_selfcheck import pool_target
    return pool_target(conn, row, gateway, cfg) or (None, None)


def _backed_off(key, revision, now):
    """True while a ``not_claimable`` refusal of ``key`` at ``revision`` holds (:data:`_refused`); otherwise clear
    it and prune expired entries (cards that left the query never come back to clear their own)."""
    held = _refused.get(key)
    if held and held[0] == revision and now < held[1]:
        return True
    _refused.pop(key, None)
    if len(_refused) > 1024:
        for stale in [k for k, (_, until) in _refused.items() if until <= now]:
            del _refused[stale]
    return False


def _repin_target(conn, row, key, gateway, cfg, mode, candidates):
    """``(labels, why, skip, refusals)`` for one selected row: a recovery reserves (``mode`` set); else the
    self-check's pool repin, else the origin repin when no candidate's pickup gate takes the card
    (:func:`kanban_worker_pickup_gate.origin_target`, t_01a1d560c6d3bc3c), else the reserve ``(None, None)``.
    ``skip``: such a card has no repin to ask now; it must not be reserved either. ``refusals``: the map a Board
    refusal of this repin is recorded in (:data:`_selfcheck_refused` or :data:`_origin_refused`), None for a run's
    ``kanban_move`` or a reserve."""
    from hermes_cli import kanban_worker_pickup_gate as pickup_gate

    if mode:
        return None, None, False, None
    labels, why = _unrefused(_selfcheck_refused, key, row, *_move_target(conn, row, gateway, cfg))
    if labels is None and (home := pickup_gate.origin_target(
            conn, row, gateway, candidates, refused_at=_origin_refused.get(key))) is not None:
        if home[0] is not None:
            _origin_refused.pop(key, None)  # asked at a new revision: the old refusal no longer holds
        return home[0], home[1], home[0] is None, _origin_refused
    return labels, why, False, _selfcheck_refused if why is not None else None


def _unrefused(refusals, key, row, labels, why):
    """``(labels, why)``, except a self-check repin (``why`` set) the Board refused at this revision becomes the
    reserve ``(None, None)``. A new revision clears the refusal so the repin is asked again."""
    if why is None:
        return labels, why
    if refusals.get(key) == row["sync_version"]:
        return None, None
    refusals.pop(key, None)
    return labels, why


def _remember_refusal(refusals, key, revision, exc):
    """Record a Board refusal of a self-check or origin repin in ``refusals`` (``not_claimable`` is
    :data:`_refused`'s backoff instead; None records nothing).

    Any 4xx but 408/425/429, coded or not (a Board or proxy without the repin route answers an uncoded 404/405 HTML
    body every tick), and a 501 are persistent. Other 5xx and 408/425/429 are outages, retried on the next tick."""
    status = getattr(exc, "status", None)
    persistent = type(status) is int and (400 <= status <= 499 or status == 501) and status not in _RETRYABLE_4XX
    if refusals is None or not persistent or rejection_code(exc) == "not_claimable":
        return
    refusals[key] = revision  # popped before every new ask, so this is the newest entry
    if len(refusals) > 1024:  # cards that left the query never come back to clear theirs
        del refusals[next(iter(refusals))]


def _record_repin(conn, task_id, gateway, labels, reply, *, reason=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_move as km
    from hermes_cli.kanban_worker_repin import REPINNED_EVENT_FLAG

    version = reply.get("version") if isinstance(reply, dict) else None
    if reason:  # the worker-first self-check's repin (the Board has committed it)
        from hermes_cli.kanban_worker_first_selfcheck import log_repin
        log_repin(task_id, labels, reason)
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, km.MOVED_EVENT, {
            "from_machine": gateway, "runs_on": labels,
            "reason": reason or "applied the run's kanban_move as the creator repin",
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
    from hermes_cli import kanban_placement as placement

    if sync.board_mode(conn) != "primary":
        return
    state = sync.sync_state(conn) or {}
    cfg = placement.config()
    pools = cfg.get("worker_pools")
    if not isinstance(pools, dict) or not pools.get(state.get("gateway_id")):
        return
    if not conn.execute("SELECT 1 FROM tasks WHERE status='ready' AND created_on=? LIMIT 1",
                        (state.get("gateway_id"),)).fetchone():
        return
    try:
        ident = client.load_identity()
        if ident.gateway != state.get("gateway_id"):
            return
        from hermes_cli import kanban_queue_reason as queue
        with ExitStack() as stack:
            # One live puller per board (t_01a1d36ddcea59fe): a sync round holding the board pulls this file, so
            # the tick reserves from it as it stands. The Board judges every reserve at its acknowledged revision.
            own = _hold_board(stack, sync._db_key(conn))
            hub = _tick_hub(client, ident)
            if own:
                try:
                    client.pull(conn, hub, state["board_id"], observers=True, partition=ident.gateway)
                except client.BoardBudgetExhausted:  # the 2 s budget ran out between or before pages
                    # A backlog outlasts the budget (a 500-change page took 4.5 s on David-MBP): the pages applied
                    # stay, and reservation still runs on a fresh budget instead of waiting for the full catch-up.
                    # A raw read timeout is no budget: it stays a recorded pull error below.
                    hub = _tick_hub(client, ident)
                except (client.HubError, client.SyncUnavailable, client.Resync, OSError) as exc:
                    queue.try_record(queue.record_pull_error, conn, ident.gateway, error=exc)
                    return
                else:
                    queue.clear_pull_streaks(ident.gateway)  # a feed-pull failure streak is no longer consecutive
            reevaluate(conn, hub, state["board_id"], ident.gateway, cfg=cfg, may_pull=own)
    except (client.HubError, client.SyncUnavailable, client.Resync, OSError):
        # Offline origin cannot remove the queue fence or manufacture an assignment.
        return


def _tick_hub(client, ident):
    return client.Hub(ident, timeout=DISPATCH_NETWORK_BUDGET_S, deadline=time.monotonic() + DISPATCH_NETWORK_BUDGET_S)


def _hold_board(stack, path) -> bool:
    """Take the board's sync lock for this tick without waiting; False when a sync round holds it."""
    from pathlib import Path
    from hermes_cli.kanban_sync_guard import SyncRoundBusy, board_sync_lock
    if path is None:
        return True  # an in-memory board: nothing else pulls it
    try:
        stack.enter_context(board_sync_lock(Path(path)))
    except SyncRoundBusy:
        return False
    return True
