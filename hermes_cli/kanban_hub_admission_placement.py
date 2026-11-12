"""Apply hub admission to an otherwise valid placement decision, before persistence."""
from __future__ import annotations

import os
from dataclasses import asdict

from hermes_cli import kanban_hub_admission as judge


def is_hub(name, cfg):
    from hermes_cli.kanban_placement import canonical_pin
    return canonical_pin(name, "hub", cfg) in ("hub", "hermes-hub")


def apply(args, decision, gateway, cfg):
    """Keep structural/non-hub outcomes unchanged; denied explicit hub pins refuse.

    Returned requires are effective persistence inputs, not just creation-time checks.
    A worker policy cannot coexist with runs_on, so hub access travels in requires.
    """
    from hermes_cli.kanban_placement import structural_reason
    from hermes_cli.kanban_worker_placement import POLICY, WAITING

    if decision.get("where") == "refuse" or structural_reason({**args, "runs_on": None}, cfg):
        return decision
    labels = args.get("runs_on")
    machines = [v[8:] for v in labels or [] if isinstance(v, str) and v.startswith("machine:")]
    machine_hub = any(is_hub(v, cfg) for v in machines)
    destination = decision.get("defer") or decision.get("where")
    local_hub = destination == "local" and is_hub(gateway, cfg)
    if not machine_hub:
        if machines or decision.get("runs_on") or decision.get("placement_policy"):
            return decision
        if not local_hub and not is_hub(destination, cfg):
            return decision
    pin = args.get("placement") or "auto"
    explicit = machine_hub or is_hub(pin, cfg)
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
    if not pools.get(gateway):
        return {"where": "refuse", "why": "hub admission denied; no worker pool configured",
                "hub_admission": audit}
    requires = args.get("requires") or []
    requires = [requires] if isinstance(requires, str) else list(requires)
    return {"where": "local", "placement_policy": POLICY, "why": WAITING,
            "requires": list(dict.fromkeys([*requires, "hub:write"])), "hub_admission": audit}
