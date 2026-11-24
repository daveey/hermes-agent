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
    elif options.get("runs_on"):
        out.update(runs_on=list(options["runs_on"]), requires=options.get("requires"))  # a queued deny
    return out if audit or out.get("placement_policy") else {}


def courier_plan(conn, args, runs_on, *, cfg=None, gateway=None, request_id=None):
    """A courier create landing on the hub: ``None`` (not hub-bound) or the admission placement.

    Runs in the receiving ``kanban_create`` before its write transaction, before the card can be
    claimed. Admitted: ``{"hub_admission": audit}`` (stays here). Denied: :func:`pool_route` with
    ``requires`` + ``hub:write`` (the worker queue, else worker-first enrollment), as a local deny;
    ``where="refuse"`` when it cannot go to the pool. A request
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
    if runs_on == [] and str(args.get("placement") or "auto").strip().lower() == "auto":
        # [] is a caller-selected any-machine lease, not hub-only execution.
        # Storage home alone must not add hub:write or narrow its eligible set.
        # An immutable earlier landing above still replays its original route.
        return None
    # validate_creation's own fences: a worker-first card cannot carry a host workspace, board or project.
    pinned = (bool(runs_on) or str(args.get("placement") or "auto").strip().lower() != "auto"
              or (args.get("workspace_kind") or "scratch") != "scratch"
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
    requires = args.get("requires") or []
    requires = [requires] if isinstance(requires, str) else list(requires)
    if pinned:
        return {"where": "refuse", "why": verdict.why, "hub_admission": audit, "pin": "hub"}
    # The worker queue exactly as for a local deny (t_01a1d0571c2f07d6); worker-first only without a live label.
    out = pool_route(args, cfg, gateway, audit, list(dict.fromkeys([*requires, "hub:write"])))
    if out.get("runs_on") is None and gateway not in (cfg.get("worker_pools") or {}):
        return {"where": "refuse", "why": verdict.why, "hub_admission": audit, "pin": "hub"}
    return out


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
    if verdict.get("skipped"):
        return f"hub admission: not judged ({(verdict.get('why') or verdict['skipped'])[:160]})"
    if verdict.get("admit"):
        return f"hub admission: admitted, {verdict.get('need')}: \"{(verdict.get('evidence') or '')[:120]}\""
    home = f"; kept home: {verdict['home']}" if verdict.get("home") else ""
    return f"hub admission: denied ({(verdict.get('why') or '')[:160]}){home}"


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
    fields = {"title": title, "body": body}
    if judge.validated(result, fields, None, sha) is None:
        detail = (judge.SELF_ASSERTION_WHY if judge.pin_quoting_admit(result, fields, None)
                  else "need or verbatim evidence")
        return {**asdict(judge.Verdict(why=f"relay-time admission verdict failed validation ({detail})",
                                       skill_sha=sha)),
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


def denied_placement(verdict, *, runs_on=None, placement_policy=None):
    """The create report for a card admission denied and re-enrolled in the pool, else None.

    Built from what was persisted (``runs_on`` / ``placement_policy``), never the pre-judge marker
    diagnostic: ``where: local`` / "home gateway marker" for a pooled card read as a hub pin and
    invited manual re-placement (self-heal t_01a1cfae6c9413c8). ``note`` is a reply-level sentence.
    """
    if not isinstance(verdict, dict) or verdict.get("admit") is not False or not (runs_on or placement_policy):
        return None
    if runs_on:
        route, target = {"runs_on": list(runs_on)}, f"a worker matching runs_on={list(runs_on)} via the board lease"
    else:
        route, target = {"placement_policy": placement_policy}, "the worker pool (waiting_for_worker_capacity)"
    return {"where": "pool", **route, "why": f"hub admission denied ({verdict.get('why') or '?'}); queued for {target}",
            "hub_admission": verdict,
            "note": f"Hub admission denied this card a hub slot: it runs on {target}, not on the hub. "
                    "Do not re-place or re-create it."}


def cli_create_fields(conn, plan, args, body, workspace_kind, workspace_path, board, *, assignee):
    """``create_task`` placement kwargs for ``hermes kanban create`` from the preflight ``plan``.

    A denied card the plan queued (``runs_on: [<pool label>, ...]``) keeps that queue only on a Board
    primary (``runs_on`` is a Board placement field); elsewhere it is enrolled worker-first instead.
    The persisted route is recorded on ``plan["persisted"]`` for the create report (:func:`cli_report`).
    """
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli.kanban_worker_placement import POLICY, cli_audit, cli_runs_on

    policy, runs_on = plan.get("placement_policy"), plan.get("runs_on")
    if runs_on is not None and sync.board_mode(conn) != "primary":
        policy, runs_on = (POLICY if plan.get("hub_admission") else policy), None
    if policy is None and runs_on is None:
        runs_on = cli_runs_on(conn, args, body, workspace_kind, workspace_path, board, assignee=assignee)
    try:  # the labels exactly as create_task persists them (sorted, deduplicated)
        persisted = wn.normalize_runs_on(runs_on)
    except ValueError:
        persisted = runs_on  # create_task rejects them before any write
    plan["persisted"] = {"runs_on": persisted, "placement_policy": policy}
    return {"requires": plan.get("requires", getattr(args, "requires", None)), "runs_on": runs_on,
            "placement_policy": policy,
            "placement_audit": with_admission(cli_audit(args, body, workspace_kind, workspace_path, board,
                                                        assignee=assignee), plan)}


def cli_report(diagnostic, plan):
    """``hermes kanban create``'s placement report after admission: a denied card reports the pool it
    was persisted to; any other card (admitted included) keeps the local diagnostic unchanged."""
    verdict = (plan or {}).get("hub_admission")
    if not diagnostic or not verdict:
        return diagnostic
    return denied_placement(verdict, **((plan or {}).get("persisted") or {})) or diagnostic


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
    routed = _decision_shortcut(args, decision, gateway, cfg, labels=labels, machine_hub=machine_hub, pin=pin,
                                queue=queue, machine_ok=machine_ok)
    if routed is not None:
        return routed
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
        return _refusal(args, verdict, audit, cfg, on_hub=on_hub, machine_hub=machine_hub, pin=pin, labels=labels)
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


def _decision_shortcut(args, decision, gateway, cfg, *, labels, machine_hub, pin, queue, machine_ok):
    """:func:`decision_card_route` for a hub-candidate decision card the caller did not pin to the hub, else None."""
    if machine_hub or labels is not None or is_hub(pin, cfg) or args.get("force_hub") or not is_decision_card(args):
        return None
    return decision_card_route(args, decision, gateway, cfg, queue=queue, machine_ok=machine_ok)


def _refusal(args, verdict, audit, cfg, *, on_hub, machine_hub, pin, labels):
    """A denied explicit hub card; a hub-origin stay-here refusal carries alternatives that work on the hub."""
    from hermes_cli.kanban_placement import _Cfg, structural_reason
    out = {"where": "refuse", "why": verdict.why, "hub_admission": audit, "pin": "hub"}
    if on_hub and not machine_hub and not is_hub(pin, cfg):
        out["remedy"] = hub_stay_remedy(structural_reason({**args, "runs_on": None}, cfg), labels,
                                        assignee=args.get("assignee") or "default", pool_profiles=_Cfg(cfg).assignees())
    return out


DECISION_CARD = "blocked_decision_card"
DECISION_WHY = ("blocked decision card (initial_status='blocked' + human_request): it holds no slot while it "
                "waits for the answer, so the hub admission judge is not consulted; once unblocked it runs {where}")


def is_decision_card(args):
    """A card created blocked on a ``human_request``: it waits for David and holds no slot meanwhile."""
    ask = args.get("human_request")
    return (str(args.get("initial_status") or "").strip().lower() == "blocked"
            and isinstance(ask, dict) and bool(ask))


def decision_card_route(args, decision, gateway, cfg, *, queue=True, machine_ok=None):
    """Placement for a hub decision card, made with no judge call; ``None`` when it must still be judged.

    The ask itself takes no slot. Its work, after unblock, goes where a denied card goes (the worker queue or
    worker-first), so skipping the judge never puts unjudged work on a hub slot. A card with a structural
    keep-local beyond the ask itself (host workspace, other board, ...) would run on the hub after unblock, so it
    is still judged. With no worker pool the card is created here, as every hub card was before admission.
    t_01a1cf9334ff49bc: a hub chat could not file a decision card at all. [daveey]
    """
    from hermes_cli.kanban_placement import portable_reason, structural_reason
    view = {k: v for k, v in args.items() if k not in ("initial_status", "human_request")}
    if structural_reason({**view, "runs_on": None}, cfg):
        return None
    requires = args.get("requires") or []
    requires = [requires] if isinstance(requires, str) else list(requires)
    if portable_reason(view, cfg):
        requires = list(dict.fromkeys([*requires, "hub:write"]))  # hub-state wording: the pool needs hub-admin
    elif not args.get("requires"):
        requires = None
    audit = {"admit": False, "need": None, "evidence": "", "skipped": DECISION_CARD,
             "why": DECISION_WHY.format(where="in the worker pool")}
    out = pool_route(view, cfg, gateway, audit, requires, queue=queue, machine_ok=machine_ok)
    if out.get("runs_on") is None and gateway not in (cfg.get("worker_pools") or {}):
        why = DECISION_WHY.format(where="here (no worker pool is configured)")
        return {**decision, "why": why, "hub_admission": {**audit, "why": why}}
    return out


ASSIGNEE_KEEP = "assignee not allowlisted"


def hub_stay_remedy(structural, labels, *, assignee=None, pool_profiles=()):
    """Refusal text for a hub-origin card the judge denied: alternatives that work here, never ``placement='local'``.

    A profile the pool does not run names that profile and the pool route: a hub chat filed Mac-only
    tailnet work as ``mac-ops`` and, told only "assignee not allowlisted", kept the hub-only profile
    (sweep slug profile-not-in-pool, t_01a1d09e42e2c4fd). [daveey]
    """
    if structural == ASSIGNEE_KEEP:
        pool = sorted(pool_profiles) or ["default"]
        pick = "default" if "default" in pool else pool[0]
        first = (f"assignee {assignee!r} is a profile no pool worker runs (pool profiles: {', '.join(pool)}); "
                 f"re-create it with assignee={pick!r} and no placement; add access constraints only if the work needs it "
                 "(runs_on=['capability:tailnet-exec'] for commands on a Mac, requires=['hub:write'] "
                 "for hub-admin changes, runs_on=['capability:hub-read'] for hub logs and DBs)")
    elif structural:
        first = (f"remove what keeps it on this host ({structural}) and omit placement so the card joins the "
                 "worker pool")
    else:
        first = "omit placement (or pass placement='auto') so the card joins the worker pool"
    if labels is not None:
        first = "drop runs_on (or name labels a pool worker carries); " + first
    return (f"On the hub, placement='local' is a hub slot and needs an admit. Instead: {first}; or pass "
            "hub_reason quoting what only the hub can do; or, for a question to David, create it with "
            "initial_status='blocked' and a human_request (it takes no slot and is never judged).")


def pool_route(args, cfg, gateway, audit, requires, *, queue=True, machine_ok=None):
    """Where a denied card goes: the worker queue (``runs_on: [<pool label>, ...]``) when ``queue``
    and ``kanban.placement.worker_queue`` names a label for this gateway that one live machine holds together
    with every coordination label, else worker-first.

    Never a ``machine:worker-N`` pin. The queue's coordination labels include ``requires``'
    (``hub:write`` -> ``capability:hub-write``), so only a hub-write worker claims it.

    Live means fresh in the Board registry (:func:`kanban_placement.label_machines`). During a Board outage the
    last good registry snapshot goes stale after ``FRESH_S``, so a denied card goes worker-first (or a denied
    mandatory pin is refused) rather than queueing on a label no machine is known to lease: fails closed.
    """
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_placement_coordination as coordination
    from hermes_cli.kanban_mandatory_pin import worker_first_refusal
    from hermes_cli.kanban_worker_placement import POLICY, QUEUED_WHY, WAITING

    label = kp._Cfg(cfg).queue_label(gateway) if queue else None
    extra = coordination.required_labels({**args, "requires": requires}, cfg)
    if extra is None:
        # The pickup claim gate (coordination.claim_refusal) refuses this text on EVERY pool worker, so neither
        # the queue nor worker-first can run it: enrolling it only stranded it ready. t_01a1d267a7e60bc2, a
        # claim-stall incident whose body says `hermes-hub outcomes --id`, sat 40 min skipped by six workers.
        # It stays home; ``requires`` stays as declared (the hub:write the deny route adds is for workers).
        out = {"where": "local", "why": f"hub admission denied, but the text {coordination.ACTION_REASON} "
                                        "no pool worker is attested for, so it stays here",
               "hub_admission": {**audit, "home": coordination.ACTION_REASON,
                                 "kept": coordination.ACTION_REASON}}
        declared = args.get("requires")
        if declared:
            # The declared list, normalized: a decision card's raw view can carry a bare string.
            out["requires"] = [declared] if isinstance(declared, str) else list(declared)
        return out
    runs_on = [label, *sorted(extra - {label})] if label else []
    # One liveness source for the whole set, as queue_decision judges it: one active, unheld, fresh pickup machine
    # must hold every label (hub:write rides as capability:hub-write), else nobody could claim the queued card.
    if label and (machine_ok or kp._machine_picks_up)(label if len(runs_on) == 1 else tuple(runs_on)):
        out = {"where": "local", "runs_on": runs_on,
               "why": QUEUED_WHY.format(label=label), "hub_admission": audit}
    elif why := worker_first_refusal(args):
        # No attested queue: a denied mandatory pin is refused, as it was while the pin counted as uncarried.
        return {"where": "refuse", "why": f"hub admission denied, and {why}", "hub_admission": audit}
    else:
        out = {"where": "local", "placement_policy": POLICY, "why": WAITING, "hub_admission": audit}
    if requires is not None:
        out["requires"] = requires
    return out
