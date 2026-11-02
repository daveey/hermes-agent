"""PR disposition gate; owner checks are board-local SQLite reads, never network I/O."""
from __future__ import annotations

from hermes_cli.kanban_workflow import DEFAULT_WORKFLOW

HUB_BATCH_CAVEAT = "The hub batch only installs merged main; it never merges PRs."
LANDING_RECOVERY = (
    "Merge now with exact-head required CI green and every review comment (bots too) answered "
    "(standing approval), or create a landing card bound to this exact PR and pass "
    "metadata.landing_owner=<card id>. To deliberately leave it open, pass "
    'metadata.pr_left_open={"reason": "<nonblank reason>"}. ' + HUB_BATCH_CAVEAT
)
OPEN_STATUSES = frozenset(DEFAULT_WORKFLOW.keys()) - {"done"}


def left_open_reason(metadata):
    raw = metadata.get("pr_left_open") if isinstance(metadata, dict) else None
    reason = raw.get("reason") if isinstance(raw, dict) else None
    return reason.strip() if isinstance(reason, str) and reason.strip() else None


def landing_disposition(conn, task_id, pr_url, metadata):
    """Resolve explicit exception or a live exact-PR owner on this connection's board."""
    if reason := left_open_reason(metadata):
        return {"pr_left_open": {"reason": reason}}
    owner = metadata.get("landing_owner") if isinstance(metadata, dict) else None
    if not isinstance(owner, str) or owner == task_id:
        return None
    row = conn.execute("SELECT status, completion_contract FROM tasks WHERE id=?", (owner,)).fetchone()
    if row and row["status"] in OPEN_STATUSES and row["completion_contract"] == pr_url:
        return {"landing_owner": owner}
    return None


def check_landing(conn, task_id, receipt, metadata):
    """Also called under the completion write transaction to close owner races."""
    if receipt.get("pr_state") != "OPEN":
        return
    disposition = landing_disposition(conn, task_id, receipt["pr_url"], metadata)
    if disposition:
        receipt["landing"] = disposition
    elif receipt["ok"] or receipt.get("waived"):
        receipt.pop("waived", None)
        receipt.pop("landing", None)
        receipt.update(ok=False, classification="open_unlanded",
                       detail=f"PR {receipt['pr_url']} is OPEN and has no valid landing disposition.",
                       recovery=LANDING_RECOVERY)


def left_open_notice(payload):
    reason = left_open_reason(payload)
    return f"PR deliberately left open: {reason}. {HUB_BATCH_CAVEAT}" if reason else ""
