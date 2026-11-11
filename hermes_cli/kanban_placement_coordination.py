"""Conservative coordination-tool portability, backed by operator Board attestations.

Labels are rollout attestations, not credentials or permission grants. Only publish them
once the installed tool has passed an actual worker proof. Unknown actions stay home.
The pickup gate checks the actual destination again, including after a pool changes.
"""
from __future__ import annotations

import math
import re
import time

REASON = "needs worker coordination capability"
ACTION_REASON = "needs home coordination action"
# Intentionally narrower than the CLI: status and pull are the reader install's proof.
_HUB = re.compile(r"\bhermes-hub\b(?!\s+client\b)(?:\s+([\w-]+))?", re.IGNORECASE)
_CROSS = re.compile(r"\bcross_task\b(?:\s+(read|list)\b)?", re.IGNORECASE)
_TOOLS = {
    "token-broker": "capability:token-broker",
    "hermes-core-wt": "capability:core-ship",
    "tailnet_exec": "capability:tailnet-exec",
}


def requirements(text):
    """Return required labels and an unsupported-action refusal, without changing text."""
    labels = set()
    for match in _HUB.finditer(text):
        if (match.group(1) or "").lower() not in ("status", "pull"):
            return labels, ACTION_REASON
        labels.add("capability:coordination-read")
    for match in _CROSS.finditer(text):
        if not match.group(1):
            return labels, ACTION_REASON
        labels.add("capability:coordination-read")
    if re.search(r"\b(?:parent|sibling)[ -]card (?:lookup|read)|\bkanban_show\b", text, re.IGNORECASE):
        labels.add("capability:coordination-read")
    for tool, label in _TOOLS.items():
        if re.search(r"\b" + re.escape(tool) + r"\b", text, re.IGNORECASE):
            labels.add(label)
    return labels, None


def destinations(cfg, target=None):
    """No pool inference for an explicit legacy heavy destination."""
    if target:
        return [target]
    from hermes_cli import kanban_capability_registry as reg
    pools = cfg.get("worker_pools")
    names = pools.get(reg.self_gateway()) if isinstance(pools, dict) else None
    if (not isinstance(names, list) or not names or len(names) > 64
            or not all(isinstance(n, str) and n.strip() for n in names)):
        return []
    return names


def attested(names, required):
    """Every possible destination needs a recent operator label; cached records age out."""
    from hermes_cli import kanban_worker_node as wn
    if not names:
        return False
    snapshot = wn.registry()
    machines = snapshot.get("machines") if isinstance(snapshot, dict) else None
    if not isinstance(machines, dict):
        return False
    now = time.time()
    for name in names:
        entry = machines.get(name)
        if not isinstance(entry, dict):
            return False
        labels = entry.get("labels")
        if (entry.get("state") != "active" or entry.get("hold") or not wn.advertises_pickup(entry)
                or not isinstance(labels, list) or not all(isinstance(v, str) for v in labels)
                or not required.issubset(labels)):
            return False
        try:
            age = now - float(entry.get("last_seen")) / 1000
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(age) or not 0 <= age <= 300:
            return False
    return True


def check(text, cfg, *, target=None, skills=None):
    required, refusal = requirements(text)
    required.update(skill_requirements(skills))
    if refusal or not required:
        return refusal
    return None if attested(destinations(cfg, target), required) else REASON


def marker_text(text):
    """Only called after check succeeds. Extra operator markers use ORIGINAL text."""
    text = _HUB.sub("", text)
    return re.sub(r"\btoken-broker\b", "", text, flags=re.IGNORECASE)


def claim_refusal(card, me):
    """Revalidate actual destination of enrolled cards, never override explicit pins."""
    if card.get("placement_policy") != "worker-first-v1":
        return None
    from hermes_cli.kanban_placement import without_evidence, without_placement_note
    text = f"{card.get('title') or ''}\n{without_evidence(without_placement_note(card.get('body') or ''))}"
    from hermes_cli.kanban_runs_on_capabilities import _loads
    return check(text, {}, target=me, skills=_loads(card.get("skills"), []))


def legacy_refusal(args):
    """Legacy automatic pins have no policy fence; do not apply coordination exemptions."""
    from hermes_cli.kanban_placement import without_evidence, without_placement_note
    text = f"{args.get('title') or ''}\n{without_evidence(without_placement_note(args.get('body') or ''))}"
    required, refusal = requirements(text)
    return bool(refusal or required or skill_requirements(args.get("skills")))


def skill_requirements(skills):
    """Known workflow pins whose runtime tools are part of this rollout."""
    if isinstance(skills, str):
        skills = [skills]
    labels = set()
    for skill in skills or []:
        if skill == "token-broker":
            labels.add("capability:token-broker")
        elif skill in ("hermes-mods-repo-core-dev", "patching-local-hermes-workflow"):
            labels.add("capability:core-ship")
    return labels
