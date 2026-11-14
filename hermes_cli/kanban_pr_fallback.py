"""All-check acceptance when a repository configures no required contexts."""
from hermes_cli.kanban_pr_check_runs import _complete
from hermes_cli.kanban_pr_merge_time import check_receipt


def complete_statuses(pages, sha):
    """gh --paginate --slurp must return pages, including one empty page for none."""
    if not isinstance(pages, list) or not pages or any(not isinstance(p, list) for p in pages):
        raise ValueError("Missing or malformed commit-status pages")
    return [{**status, "sha": sha} for page in pages for status in page]


def fallback_outcomes(receipt, required, outcomes, runs, statuses, suites, *, merged):
    """Keep required semantics, otherwise require every current producer's success."""
    if required:
        return outcomes
    suite_rows = _complete(suites, "check_suites", "check-suite")
    sha = receipt["head_sha"]
    latest = {}
    for status in sorted(statuses, key=lambda s: s["id"]):
        latest[(status["context"], (status.get("creator") or {}).get("id"))] = status
    receipt["checks"] = [check_receipt(c, sha) for c in runs + list(latest.values())]
    if not merged:
        for suite in suite_rows:
            # An empty latest attempt can be queued CI, not absence, even if
            # other producers already succeeded. Required-context logic is separate.
            if suite["latest_check_runs_count"] == 0:
                receipt["checks"].append({"name": f"check suite {suite['id']}",
                    "classification": "missing", "head_sha": sha})
    return [c["classification"] for c in receipt["checks"]]


def mark_absence(receipt, required, outcomes, suites, where):
    """Waive only proven absence, never missing/pending or post-merge-only CI."""
    from hermes_cli.kanban_pr_acceptance import NO_REQUIRED_CHECKS_DETAIL, _REPOINT_HINT

    if required or outcomes or receipt["post_merge_checks"]:
        return
    if _complete(suites, "check_suites", "check-suite"):
        return
    receipt.update(classification="no_required_checks", detail=NO_REQUIRED_CHECKS_DETAIL.format(
        where=where, repoint=_REPOINT_HINT))
