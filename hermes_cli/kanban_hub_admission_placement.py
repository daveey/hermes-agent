"""Apply hub admission to an otherwise valid placement decision, before persistence."""
from __future__ import annotations

import os
from dataclasses import asdict

from hermes_cli import kanban_hub_admission as judge


def is_hub(name, cfg):
    from hermes_cli.kanban_placement import canonical_pin
    return canonical_pin(name, "hub", cfg) in ("hub", "hermes-hub")


def hub_only(labels, cfg):
    """True when only the hub can claim a card with these lease labels (``[]`` = any machine counts).

    An unreadable registry counts as hub-only: the judge is consulted rather than skipped.
    """
    from hermes_cli import kanban_worker_node as wn
    if not labels:
        return True
    if any(v.startswith("machine:") and not is_hub(v[8:], cfg) for v in labels):
        return False  # the named non-hub machine claims it
    try:
        machines = (wn.registry() or {}).get("machines") or {}
    except Exception:  # health: allow BLE001 -- an unknown registry is judged, never waved through
        return True
    if not machines:
        return True
    return not any(set(labels) <= wn.effective_labels(entry, name) for name, entry in machines.items()
                   if not is_hub(name, cfg) and (entry or {}).get("state", "active") == "active")


def _verdict(fields, hub_reason=None):
    try:
        return judge.admit(fields, hub_reason=hub_reason)
    except Exception:  # health: allow BLE001 -- a failed judge is a deny
        return judge.Verdict()


def _prior_landing(conn, request_id):
    """The placement an earlier apply of the same courier request persisted, or None.

    A re-applied request (crash after commit, adapter timeout) must rebuild the identical
    immutable envelope; judging again could flip the verdict and raise origin_creation_conflict.
    """
    import json
    import sqlite3
    if conn is None or not request_id:
        return None
    try:
        row = conn.execute("SELECT intent_json FROM origin_creation_intents WHERE request_id = ?",
                           (request_id,)).fetchone()
        options = (json.loads(row[0]) or {}).get("options") or {} if row else None
    except (sqlite3.OperationalError, TypeError, ValueError):
        return None  # no intents table yet, or an unreadable row: create_with_origin reports it
    if not isinstance(options, dict):
        return None
    from hermes_cli.kanban_worker_placement import POLICY, WAITING
    audit = (options.get("placement_audit") or {}).get("hub_admission")
    out = {"where": "local", "why": "courier replay", **({"hub_admission": audit} if audit else {})}
    if options.get("placement_policy") == POLICY:
        out.update(placement_policy=POLICY, why=WAITING, requires=options.get("requires"))
    return out if audit or out.get("placement_policy") else {}


def courier_plan(conn, args, runs_on, *, cfg=None, gateway=None, request_id=None):
    """A courier create landing on the hub: ``None`` (not hub-bound) or the admission placement.

    Runs in the receiving ``kanban_create`` before its write transaction, before the card can be
    claimed. Admitted: ``{"hub_admission": audit}`` (stays here). Denied: worker-first enrollment
    with ``requires`` + ``hub:write``; ``where="refuse"`` when it cannot go to the pool. A request
    already applied reuses its stored placement and makes no model call.
    """
    from hermes_cli import kanban_placement as kp
    from hermes_cli.kanban_worker_placement import POLICY, WAITING
    cfg = kp.config() if cfg is None else cfg
    gateway = kp.reg.self_gateway() if gateway is None else gateway
    if not is_hub(gateway, cfg):
        return None
    machines = [v[8:] for v in runs_on or [] if v.startswith("machine:")]
    if runs_on is not None and not any(is_hub(m, cfg) for m in machines) and not hub_only(runs_on, cfg):
        return None  # the lease labels let a worker claim it: not a hub slot
    prior = _prior_landing(conn, request_id)
    if prior is not None:
        return prior or None
    # validate_creation's own fences: a worker-first card cannot carry a host workspace, board or project.
    pinned = (runs_on is not None or (args.get("workspace_kind") or "scratch") != "scratch"
              or args.get("workspace_path") or args.get("project_id")
              or args.get("board") not in (None, "", "default"))
    if conn is not None and conn.in_transaction:
        verdict = judge.Verdict()  # never a model call under the SQLite write lock
    else:
        # The origin's routing note names *its* host needs (Chrome profile, Keychain): never hub evidence.
        verdict = _verdict({**args, "body": kp.without_placement_note(args.get("body") or "")},
                           args.get("hub_reason"))
    audit = {**asdict(verdict), "entry": "courier"}
    if verdict.admit:
        return {"hub_admission": audit}
    if pinned or gateway not in (cfg.get("worker_pools") or {}):
        return {"where": "refuse", "why": verdict.why, "hub_admission": audit, "pin": "hub"}
    requires = args.get("requires") or []
    requires = [requires] if isinstance(requires, str) else list(requires)
    # Courier creation never takes the worker queue (queue_decision's own rule): worker-first.
    return pool_route(args, cfg, gateway, audit, list(dict.fromkeys([*requires, "hub:write"])), queue=False)


def gate_move(conn, task_id, runs_on, *, cfg=None):
    """``None`` when a move does not target the hub or the judge admits it, else a refusal dict.

    Runs before the mover's write transaction (never a model call under the SQLite lock) and
    records every verdict as a ``hub_admission`` event on the card.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_worker_node as wn

    cfg = kp.config() if cfg is None else cfg
    try:
        labels = wn.normalize_runs_on(runs_on) or []
    except ValueError:  # malformed labels: still judge any hub machine named in them (fail closed)
        raw = [runs_on] if isinstance(runs_on, str) else list(runs_on or [])
        labels = [p.strip() for v in raw if isinstance(v, str) for p in v.split(",")]
    if not any(v.lower().startswith("machine:") and is_hub(v[8:], cfg) for v in labels):
        return None
    row = conn.execute("SELECT title, body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None  # the mover's own check reports an unknown card
    if conn.in_transaction:  # never a model call under the SQLite write lock; still recorded
        audit = {**asdict(judge.Verdict()), "entry": "move", "runs_on": labels}
        kb._append_event(conn, task_id, "hub_admission", audit)
        return {"ok": False, "reason_code": "hub_admission_unavailable",
                "error": "hub admission cannot run inside an open transaction", "hub_admission": audit}
    try:
        verdict = judge.admit({"title": row["title"], "body": row["body"]})
    except Exception:  # health: allow BLE001 -- a failed judge is a deny
        verdict = judge.Verdict()
    audit = {**asdict(verdict), "entry": "move", "runs_on": labels}
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "hub_admission", audit)
    if verdict.admit:
        return None
    return {"ok": False, "reason_code": "hub_admission_denied",
            "error": f"hub admission denied: {verdict.why}", "hub_admission": audit}


def latest_verdict(conn, task_id):
    """The card's most recent ``hub_admission`` event payload (``kanban_show`` / queue notes)."""
    import json
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'hub_admission' "
                       "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if row is None or not row[0]:
        return None
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def describe(verdict):
    """One line: denied (why) / admitted: need, "evidence" / forced by operator."""
    if not verdict:
        return ""
    if verdict.get("forced"):
        return f"hub admission: forced by operator (session {verdict.get('session') or '?'})"
    if verdict.get("admit"):
        return f"hub admission: admitted, {verdict.get('need')}: \"{(verdict.get('evidence') or '')[:120]}\""
    return f"hub admission: denied ({(verdict.get('why') or '')[:160]})"


def _event_verdict(events):
    verdicts = [e.payload for e in events if e.kind == "hub_admission" and isinstance(e.payload, dict)]
    return verdicts[-1] if verdicts else None


def show_json(events):
    """``hermes kanban show --json`` fields: the newest verdict, when the card has one."""
    verdict = _event_verdict(events)
    return {"hub_admission": verdict} if verdict else {}


def print_show_field(events, field):
    """``hermes kanban show``: a ``hub:`` line saying why this card is (or is not) on the hub."""
    verdict = _event_verdict(events)
    if verdict:
        field("hub", describe(verdict))


def cli_preflight(args, body, workspace_kind, workspace_path, board, *, assignee):
    """``hermes kanban create`` admission before any connection: ``(plan, exit code or None)``.

    A refusal is printed here (``--json``: ``code hub_admission_denied``) and returns exit code 2.
    """
    import sys
    from hermes_cli.kanban_output import _print_json
    plan = cli_plan(args, body, workspace_kind, workspace_path, board, assignee=assignee)
    if plan.get("where") != "refuse":
        return plan, None
    verdict = plan.get("hub_admission") or {}
    if getattr(args, "json", False):
        _print_json({"ok": False, "created": False, "code": "hub_admission_denied",
                     "error": plan.get("why", ""), "hub_admission": plan.get("hub_admission")})
    else:
        print(f"kanban: hub_admission_denied: {plan.get('why', '')} (no card was created; "
              f"need={verdict.get('need')!r}). Drop the hub pin, or give --hub-reason "
              "quoting what only the hub can do.", file=sys.stderr)
    return plan, 2


def owner_verdict(title, body, *, need):
    """Judge a capability owner card's text at relay time (never inside a wait-check probe)."""
    return {**asdict(_verdict({"title": title, "body": body})), "entry": "capability_owner",
            "capability": need}


def stored_owner_verdict(entry, title, body):
    """The relay-time verdict for the owner card being created now, or None when it cannot be used.

    None means the judge was unavailable (retryable). A stored verdict that fails the judge's own
    validation (need off the allowlist, evidence not verbatim in ``title``/``body``) is a final
    deny, never a retry: re-relaying would replay the same cached verdict on the same text.
    """
    audit = (entry or {}).get("hub_admission")
    if not isinstance(audit, dict) or not isinstance(audit.get("admit"), bool):
        return None
    if audit.get("why") == judge.Verdict().why:
        return None  # the judge was unavailable at relay time: retry, never a permanent answer
    sha = audit["skill_sha"] if isinstance(audit.get("skill_sha"), str) else ""
    result = {k: audit.get(k) for k in ("admit", "need", "evidence", "why")}
    if judge.validated(result, {"title": title, "body": body}, None, sha) is None:
        return {**asdict(judge.Verdict(why="relay-time admission verdict failed validation (need or verbatim "
                                           "evidence)", skill_sha=sha)),
                "entry": audit.get("entry") or "capability_owner", "capability": audit.get("capability"),
                "invalid": True}
    return audit


def mismatched_owner_verdict(entry, need):
    """Final deny for relayed owner text that differs from the hub's own rebuild of it."""
    audit = (entry or {}).get("hub_admission")
    audit = audit if isinstance(audit, dict) else {}
    sha = audit["skill_sha"] if isinstance(audit.get("skill_sha"), str) else ""
    return {**asdict(judge.Verdict(why="relayed owner text does not match the hub's rebuild of it",
                                   skill_sha=sha)),
            "entry": "capability_owner", "capability": need, "invalid": True}


def gate_owner(conn, waiting_task_id, title, body, *, need, audit=None):
    """Judge (or take ``audit`` judged at relay time) a hub-bound owner card; record the verdict.

    Returns the audit dict (``admit`` says whether the owner may run on the hub). Inside an open
    transaction no model call is made (deny), but the verdict is still recorded in that transaction.
    """
    from hermes_cli import kanban_db as kb
    if audit is None:
        if conn.in_transaction:  # never a model call under the SQLite write lock
            audit = {**asdict(judge.Verdict()), "entry": "capability_owner", "capability": need}
        else:
            audit = owner_verdict(title, body, need=need)
    if conn.in_transaction:
        kb._append_event(conn, waiting_task_id, "hub_admission", audit)
    else:
        with kb.write_txn(conn):
            kb._append_event(conn, waiting_task_id, "hub_admission", audit)
    return audit


def with_admission(audit, plan):
    """Carry the preflight verdict into ``record_created`` (emits the ``hub_admission`` event)."""
    if plan and plan.get("hub_admission"):
        return {**(audit or {}), "hub_admission": plan["hub_admission"]}
    return audit


def cli_create_fields(conn, plan, args, body, workspace_kind, workspace_path, board, *, assignee):
    """``create_task`` placement kwargs for ``hermes kanban create`` from the preflight ``plan``.

    A denied card the plan queued (``runs_on: [<pool label>, ...]``) keeps that queue only on a Board
    primary (``runs_on`` is a Board placement field); elsewhere it is enrolled worker-first instead.
    """
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_worker_placement import POLICY, cli_audit, cli_runs_on

    policy, runs_on = plan.get("placement_policy"), plan.get("runs_on")
    if runs_on is not None and sync.board_mode(conn) != "primary":
        policy, runs_on = (POLICY if plan.get("hub_admission") else policy), None
    if policy is None and runs_on is None:
        runs_on = cli_runs_on(conn, args, body, workspace_kind, workspace_path, board, assignee=assignee)
    return {"requires": plan.get("requires", getattr(args, "requires", None)), "runs_on": runs_on,
            "placement_policy": policy,
            "placement_audit": with_admission(cli_audit(args, body, workspace_kind, workspace_path, board,
                                                        assignee=assignee), plan)}


def cli_plan(args, body, workspace_kind, workspace_path, board, *, assignee=None):
    """``hermes kanban create``: the worker-first decision plus admission, computed before any write."""
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli.kanban_worker_placement import _cli_fields, creation_decision

    cfg, gateway = kp.config(), kp.reg.self_gateway()
    fields = _cli_fields(args, body, workspace_kind, workspace_path, board, assignee=assignee)
    if fields.get("runs_on") is not None:
        try:
            fields["runs_on"] = list(wn.normalize_runs_on(fields["runs_on"]) or [])
        except ValueError:
            return {"where": "local", "why": "invalid runs_on (create reports it)"}
    decision = creation_decision(fields, cfg, gateway, "auto", None) if cfg.get("worker_pools") else None
    return apply(fields, decision or {"where": "local", "why": "cli create"}, gateway, cfg)


def apply(args, decision, gateway, cfg, *, origin_intent=None, queue=True, machine_ok=None):
    """Keep structural/non-hub outcomes unchanged; denied explicit hub pins refuse.

    Returned requires are effective persistence inputs, not just creation-time checks.
    A worker policy cannot coexist with runs_on, so hub access travels in requires.
    Courier applies (``origin_intent``: another gateway's routed create landing here) are
    judged by :func:`courier_plan` on the receiving hub, before its create transaction.
    """
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli.kanban_placement import structural_reason
    from hermes_cli.kanban_worker_placement import POLICY, WAITING

    if origin_intent is not None:
        return decision  # judged where it lands, outside the courier's txn: see courier_plan
    if decision.get("where") == "refuse":
        return decision  # structural refusals make no model call
    try:
        # The exact labels the create persists: a comma string or padded label is still machine:hub.
        labels = wn.normalize_runs_on(args.get("runs_on"))
    except ValueError:
        return decision  # the create itself rejects invalid labels before any write
    machines = [v[8:] for v in labels or [] if v.startswith("machine:")]
    machine_hub = any(is_hub(v, cfg) for v in machines)
    destination = decision.get("defer") or decision.get("where")
    on_hub = is_hub(gateway, cfg)
    local_hub = destination == "local" and on_hub
    pin = str(args.get("placement") or "auto").strip()
    # On the hub, "stay here" is an explicit hub pin: a deny refuses instead of moving the card.
    explicit = machine_hub or is_hub(pin, cfg) or (local_hub and pin.lower() in ("local", "here"))
    if not machine_hub:
        if labels is not None:
            # Lease labels: judged only when created here and nothing but the hub can claim them.
            if not (on_hub and hub_only(labels, cfg)):
                return decision
            explicit = True
        elif decision.get("runs_on") or decision.get("placement_policy"):
            return decision  # already enrolled or pinned away from the hub
        # A foreign gateway's automatic route to the hub is judged when it lands (courier_plan);
        # at the origin only explicit hub pins are, so non-hub origins make zero model calls.
        elif not local_hub and not (explicit and is_hub(destination, cfg)):
            return decision
    if structural_reason({**args, "runs_on": None}, cfg):
        explicit = True  # a host workspace, other board or uncarried option cannot go to the pool
    if args.get("force_hub"):
        if os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_KANBAN_WORKER_TASK"):
            return {"where": "refuse", "why": "force_hub is unavailable to dispatcher workers"}
        verdict = judge.Verdict(admit=True, why="operator override", forced=True)
    else:
        from hermes_cli.kanban_placement import without_placement_note
        # A routed card's placement note names the origin's host needs (Chrome, Keychain): never hub evidence.
        verdict = _verdict({**args, "body": without_placement_note(args.get("body") or "")}, args.get("hub_reason"))
    audit = asdict(verdict)
    if verdict.forced:
        audit["session"] = os.environ.get("HERMES_SESSION_ID", "")
    if verdict.admit:
        return {**decision, "hub_admission": audit}
    if explicit:
        return {"where": "refuse", "why": verdict.why, "hub_admission": audit, "pin": "hub"}
    requires = args.get("requires") or []
    requires = [requires] if isinstance(requires, str) else list(requires)
    # Board contract (hermes-hub #90): requires hub:write -> capability:hub-write at reservation and
    # claim, and the worker queue's coordination labels; only for cards whose text hit a hub-state
    # marker (they may still need hub-admin).
    from hermes_cli.kanban_placement import portable_reason
    if portable_reason(args, cfg):
        # Only when hub:write is added: an omitted requires must stay omitted (declaration gate).
        requires = list(dict.fromkeys([*requires, "hub:write"]))
    elif not args.get("requires"):
        requires = None
    out = pool_route(args, cfg, gateway, audit, requires, queue=queue, machine_ok=machine_ok)
    if out.get("runs_on") is None and gateway not in (cfg.get("worker_pools") or {}):
        return {"where": "refuse", "why": "hub admission denied; no worker pool configured",
                "hub_admission": audit}
    return out


def pool_route(args, cfg, gateway, audit, requires, *, queue=True, machine_ok=None):
    """Where a denied card goes: the worker queue (``runs_on: [<pool label>, ...]``) when ``queue``
    and ``kanban.placement.worker_queue`` names a live label for this gateway, else worker-first.

    Never a ``machine:worker-N`` pin. The queue's coordination labels include ``requires``'
    (``hub:write`` -> ``capability:hub-write``), so only a hub-write worker claims it.
    """
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_placement_coordination as coordination
    from hermes_cli.kanban_worker_placement import POLICY, QUEUED_WHY, WAITING

    label = kp._Cfg(cfg).queue_label(gateway) if queue else None
    extra = coordination.required_labels({**args, "requires": requires}, cfg) if label else None
    if label and extra is not None and (machine_ok or kp._machine_picks_up)(label):
        out = {"where": "local", "runs_on": [label, *sorted(extra - {label})],
               "why": QUEUED_WHY.format(label=label), "hub_admission": audit}
    else:
        out = {"where": "local", "placement_policy": POLICY, "why": WAITING, "hub_admission": audit}
    if requires is not None:
        out["requires"] = requires
    return out
