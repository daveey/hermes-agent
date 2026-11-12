"""Apply hub admission to an otherwise valid placement decision, before persistence."""
from __future__ import annotations

import os
from dataclasses import asdict

from hermes_cli import kanban_hub_admission as judge


def is_hub(name, cfg):
    from hermes_cli.kanban_placement import canonical_pin
    return canonical_pin(name, "hub", cfg) in ("hub", "hermes-hub")


def gate_move(conn, task_id, runs_on, *, cfg=None):
    """``None`` when a move does not target the hub or the judge admits it, else a refusal dict.

    Runs before the mover's write transaction (never a model call under the SQLite lock) and
    records every verdict as a ``hub_admission`` event on the card.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_placement as kp

    raw = [runs_on] if isinstance(runs_on, str) else list(runs_on or [])
    cfg = kp.config() if cfg is None else cfg
    labels = [str(v).strip() for v in raw if isinstance(v, str)]
    if not any(v.lower().startswith("machine:") and is_hub(v[8:], cfg) for v in labels):
        return None
    row = conn.execute("SELECT title, body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None  # the mover's own check reports an unknown card
    if conn.in_transaction:
        return {"ok": False, "reason_code": "hub_admission_unavailable",
                "error": "hub admission cannot run inside an open transaction"}
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


def gate_owner(conn, waiting_task_id, title, body, *, need):
    """Judge a capability owner card bound for the hub; records the verdict on the waiting card.

    Returns the audit dict (``admit`` says whether the owner may run on the hub).
    """
    from hermes_cli import kanban_db as kb
    if conn.in_transaction:  # never a model call under the SQLite write lock
        return {**asdict(judge.Verdict()), "entry": "capability_owner", "capability": need}
    try:
        verdict = judge.admit({"title": title, "body": body})
    except Exception:  # health: allow BLE001 -- a failed judge is a deny
        verdict = judge.Verdict()
    audit = {**asdict(verdict), "entry": "capability_owner", "capability": need}
    with kb.write_txn(conn):
        kb._append_event(conn, waiting_task_id, "hub_admission", audit)
    return audit


def with_admission(audit, plan):
    """Carry the preflight verdict into ``record_created`` (emits the ``hub_admission`` event)."""
    if plan and plan.get("hub_admission"):
        return {**(audit or {}), "hub_admission": plan["hub_admission"]}
    return audit


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


def apply(args, decision, gateway, cfg, *, origin_intent=None):
    """Keep structural/non-hub outcomes unchanged; denied explicit hub pins refuse.

    Returned requires are effective persistence inputs, not just creation-time checks.
    A worker policy cannot coexist with runs_on, so hub access travels in requires.
    Courier applies (``origin_intent``: another gateway's routed create landing here) keep
    today's behaviour: the origin decided the route, and worker-first cannot carry a courier.
    """
    from hermes_cli.kanban_placement import structural_reason
    from hermes_cli.kanban_worker_placement import POLICY, WAITING

    if origin_intent is not None:
        return decision
    if decision.get("where") == "refuse" or structural_reason({**args, "runs_on": None}, cfg):
        return decision
    labels = args.get("runs_on")
    machines = [v[8:] for v in labels or [] if isinstance(v, str) and v.startswith("machine:")]
    machine_hub = any(is_hub(v, cfg) for v in machines)
    destination = decision.get("defer") or decision.get("where")
    local_hub = destination == "local" and is_hub(gateway, cfg)
    pin = args.get("placement") or "auto"
    explicit = machine_hub or is_hub(pin, cfg)
    if not machine_hub:
        # Any explicit runs_on (even [] = any machine) is executed by the Board lease, not this slot.
        if labels is not None or decision.get("runs_on") or decision.get("placement_policy"):
            return decision
        # A foreign gateway's automatic route to the hub is the origin's routing decision (it lands
        # as a courier apply, unchanged); only explicit hub pins are judged at the origin, so
        # non-hub origins make zero model calls unless the caller names the hub.
        if not local_hub and not (explicit and is_hub(destination, cfg)):
            return decision
    if args.get("force_hub"):
        if os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_KANBAN_WORKER_TASK"):
            return {"where": "refuse", "why": "force_hub is unavailable to dispatcher workers"}
        verdict = judge.Verdict(admit=True, why="operator override", forced=True)
    else:
        try:
            verdict = judge.admit(args, hub_reason=args.get("hub_reason"))
        except Exception:  # health: allow BLE001 -- never let caller fallback admit a failed judge
            verdict = judge.Verdict()
    audit = asdict(verdict)
    if verdict.forced:
        audit["session"] = os.environ.get("HERMES_SESSION_ID", "")
    if verdict.admit:
        return {**decision, "hub_admission": audit}
    if explicit:
        return {"where": "refuse", "why": verdict.why, "hub_admission": audit, "pin": "hub"}
    pools = cfg.get("worker_pools") or {}
    if gateway not in pools:
        return {"where": "refuse", "why": "hub admission denied; no worker pool configured",
                "hub_admission": audit}
    requires = args.get("requires") or []
    requires = [requires] if isinstance(requires, str) else list(requires)
    # Board contract (hermes-hub #90): requires hub:write -> capability:hub-write at reservation and
    # claim. runs_on stays owned by the worker-first reservation, so the label travels in requires,
    # and only for cards whose text hit a hub-state marker (they may still need hub-admin).
    from hermes_cli.kanban_placement import portable_reason
    out = {"where": "local", "placement_policy": POLICY, "why": WAITING, "hub_admission": audit}
    if portable_reason(args, cfg):
        # Only when hub:write is added: an omitted requires must stay omitted (declaration gate).
        out["requires"] = list(dict.fromkeys([*requires, "hub:write"]))
    return out
