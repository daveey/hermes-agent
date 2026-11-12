"""Structural portability evidence and creation diagnostics, independent of routing."""
from __future__ import annotations

import re


def portability(args: dict, cfg=None) -> tuple[str | None, str | None]:
    """Return the stable reason and its actual marker evidence in one classifier pass.

    Keep ordering and exemptions identical for admission and diagnostics. In particular,
    never rescan raw text for an explanation after evidence/coordination filtering.
    """
    from hermes_cli import kanban_placement as p
    from hermes_cli import kanban_placement_coordination as coordination
    from hermes_cli.kanban_carried_evidence import without_carried_inputs
    from hermes_cli.kanban_published_worktree import without_published_worktrees

    c = p._Cfg(p.config() if cfg is None else cfg)
    if reason := p.structural_reason(args, c.raw):
        return reason, None
    reqs = args.get("requires") or []
    reqs = [reqs] if isinstance(reqs, str) else list(reqs)
    if refusal := coordination.board_refusal(reqs):
        return refusal, None
    title = args.get("title") or ""
    body = p.without_evidence(p.without_placement_note(args.get("body") or ""))
    if hit := p._local_marker(title, body, args.get("skills"), c):
        return "local marker", hit
    full = without_carried_inputs(f"{title}\n{body}", c.raw)
    if refusal := coordination.check(full, c.raw, skills=args.get("skills"), requires=reqs):
        return refusal, None
    marker_full = coordination.access_text(coordination.marker_text(full), c.raw, requires=reqs)
    extra = c.raw.get("extra_home_markers")
    exemptible = (p.HUB_CLI_COMMAND, r"\btoken-broker\b", r"\bssh\b", r"\bon the hub\b", r"\bthe hub's\b",
                  r"\bjournalctl\b", r"\bkanban\.db\b")
    pats = [(rx, marker_full if rx in exemptible else full)
            for rx in p.HOME_GATEWAY_MARKERS if rx != p.HOST_WORKTREE_PATH]
    pats += [(rx, full) for rx in extra] if isinstance(extra, list) else []
    for rx, text in pats:
        try:
            if match := re.search(rx, text, re.IGNORECASE):
                return "home gateway marker", f"mentions {match.group(0)!r}"
        except re.error:
            continue
    # Avoid filesystem/network probes when a cheap marker already pins the card.
    if re.search(p.HOST_WORKTREE_PATH, full, re.IGNORECASE):
        if match := re.search(p.HOST_WORKTREE_PATH, without_published_worktrees(full), re.IGNORECASE):
            return "home gateway marker", f"mentions {match.group(0)!r}"
    return None, None


def local_why(args: dict, cfg=None, *, fallback="no placement route for this gateway") -> str:
    reason, evidence = portability(args, cfg)
    return f"{reason}: {evidence}" if evidence else reason or fallback


def routeless_why(args: dict, cfg, origin_intent) -> str:
    """Explain auto structural rejection without relabelling manual/courier routing."""
    fallback = "no placement route for this gateway"
    if args.get("runs_on") is not None or origin_intent is not None:
        return fallback
    return local_why(args, cfg, fallback=fallback)


def cli_creation(args, body, workspace_kind, workspace_path, board, task, *, skip=False) -> dict | None:
    """Describe the CLI's actual local create; never call the tool's routing decision.

    Manual pins and worker enrollment take precedence over structural diagnostics.
    This is a fresh-create observation, not a replay or a new placement decision.
    """
    from hermes_cli.kanban_worker_placement import WAITING, _cli_fields

    if skip:
        return None
    result = {"where": "local"}
    if task.placement_policy:
        return {**result, "placement_policy": task.placement_policy, "why": WAITING}
    runs_on = getattr(args, "runs_on", None)
    if runs_on is not None:
        return {**result, "runs_on": runs_on, "why": "execution labels specified by the caller"}
    try:
        fields = _cli_fields(args, body, workspace_kind, workspace_path, board, assignee=task.assignee)
        result["why"] = local_why(fields, fallback="CLI creates on this gateway")
    except Exception:  # health: allow BLE001 -- diagnostics cannot fail an already committed create
        result["why"] = "portability diagnostic failed"
    return result


def cli_json_fields(diagnostic) -> dict:
    return {"placement": diagnostic} if diagnostic else {}


def print_cli_creation(diagnostic) -> None:
    if diagnostic:
        print(f"Placement: {diagnostic['why']}")
