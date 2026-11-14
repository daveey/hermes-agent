"""All-check acceptance when a repository configures no required contexts."""
from datetime import UTC, datetime, timedelta

from hermes_cli.kanban_pr_check_runs import _complete
from hermes_cli.kanban_pr_merge_time import check_receipt, job_attempt, timestamp

# A just-created empty suite may be CI that has not started its jobs yet.
IDLE_SUITE_GRACE = timedelta(minutes=15)
# A queued empty Actions suite can be a concurrency-held workflow: never idle.
GITHUB_ACTIONS_APP_ID = 15368
# GitHub branch protection accepts these completed conclusions as passing.
_PASSING_CONCLUSIONS = ("neutral", "skipped")


def complete_statuses(pages, sha):
    """gh --paginate --slurp must return pages, including one empty page for none."""
    if not isinstance(pages, list) or not pages or any(not isinstance(p, list) for p in pages):
        raise ValueError("Missing or malformed commit-status pages")
    return [{**status, "sha": sha} for page in pages for status in page]


def _app_id(row):
    app = row.get("app")
    return app.get("id") if isinstance(app, dict) else None


def idle_suites(suite_rows, runs, now):
    """Installed apps that never run CI leave 0-run suites ``queued`` forever.

    Such a suite is idle (ignored) only when nothing suggests real CI: it is
    still ``queued``, its app has no runs or non-empty suites on this commit,
    and it is older than the grace period. Anything unreadable stays non-idle.
    """
    active = {_app_id(r) for r in runs}
    active |= {_app_id(s) for s in suite_rows if s.get("latest_check_runs_count") != 0}
    idle = []
    for suite in suite_rows:
        app = _app_id(suite)
        if suite.get("latest_check_runs_count") != 0 or suite.get("status") != "queued":
            continue
        if app is None or app in active or app == GITHUB_ACTIONS_APP_ID:
            continue
        try:
            age = now - timestamp(suite["created_at"].replace("Z", "+00:00"))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if age >= IDLE_SUITE_GRACE:
            idle.append(suite)
    return idle


def _grade(check, sha):
    evidence = check_receipt(check, sha)
    # _classify grades stale/pending before the conclusion map, so an "infra"
    # row with a passing conclusion is a completed exact-head run.
    if evidence["classification"] == "infra" and evidence["conclusion"] in _PASSING_CONCLUSIONS:
        evidence["classification"] = "success"
    return evidence


def fallback_outcomes(receipt, required, outcomes, runs, statuses, suites, *, merged, now=None):
    """Keep required semantics, otherwise require every current producer's success."""
    if required:
        return outcomes
    suite_rows = _complete(suites, "check_suites", "check-suite")
    sha = receipt["head_sha"]
    latest = {}
    for status in sorted(statuses, key=lambda s: s["id"]):
        latest[(status["context"], (status.get("creator") or {}).get("id"))] = status
    receipt["checks"] = [_grade(c, sha) for c in runs + list(latest.values())]
    idle = idle_suites(suite_rows, runs, now or datetime.now(UTC))
    receipt["idle_suites"] = [{"id": s["id"], "app": (s.get("app") or {}).get("slug"),
                               "status": s.get("status"), "created_at": s.get("created_at")}
                              for s in idle]
    idle_ids = {s["id"] for s in idle}
    for suite in suite_rows:
        if suite["latest_check_runs_count"] != 0 or suite["id"] in idle_ids:
            continue
        failed = _failed_empty_suite(suite, sha)
        if failed and merged and _created_after_merge(suite, receipt.get("merged_at")):
            receipt["post_merge_checks"].append(failed)
        elif failed:
            # Workflow failed before creating jobs: red on both paths.
            receipt["checks"].append(failed)
        elif not merged:
            # An empty latest attempt can be queued CI, not absence, even if
            # other producers already succeeded. Required-context logic is separate.
            receipt["checks"].append({"name": f"check suite {suite['id']}",
                "classification": "missing", "head_sha": sha})
    return [c["classification"] for c in receipt["checks"]]


def _failed_empty_suite(suite, sha):
    """A completed 0-run suite that did not pass (e.g. invalid workflow file)."""
    if suite.get("status") != "completed" or suite.get("conclusion") in ("success", *_PASSING_CONCLUSIONS):
        return None
    return {"name": f"check suite {suite['id']}", "id": suite["id"], "classification": "failure",
            "conclusion": suite.get("conclusion"), "completed_at": suite.get("updated_at"),
            "head_sha": sha}


def _created_after_merge(suite, merged_at):
    """Only a suite provably created after the merge is post-merge (else fail closed)."""
    try:
        return timestamp(suite["created_at"].replace("Z", "+00:00")) > timestamp(
            merged_at.replace("Z", "+00:00"))
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def actions_attempts(api, repo, profile_home):
    """Resolve a check run to its Actions (run id, run_attempt) to prove a rerun.

    Non-Actions runs and unreadable jobs are unprovable (None): the earlier run
    then stays as evidence. Transport failures still propagate (infra).
    """
    from hermes_cli.kanban_pr_acceptance import _GateAuthError

    def attempt_of(check):
        if (check.get("app") or {}).get("id") != GITHUB_ACTIONS_APP_ID:
            return None
        try:
            job = api(f"repos/{repo}/actions/jobs/{int(check['id'])}", profile_home=profile_home)
        except _GateAuthError:
            return None
        return job_attempt(job, check) if isinstance(job, dict) else None
    return attempt_of


def mark_absence(receipt, required, outcomes, suites, where):
    """Waive only proven absence, never missing/pending or post-merge-only CI.

    Idle third-party suites (see ``idle_suites``) are absence, not evidence.
    """
    from hermes_cli.kanban_pr_acceptance import NO_REQUIRED_CHECKS_DETAIL, _REPOINT_HINT

    if required or outcomes or receipt["post_merge_checks"]:
        return
    idle_ids = {s["id"] for s in receipt.get("idle_suites", [])}
    if any(s["id"] not in idle_ids for s in _complete(suites, "check_suites", "check-suite")):
        return
    receipt.update(classification="no_required_checks", detail=NO_REQUIRED_CHECKS_DETAIL.format(
        where=where, repoint=_REPOINT_HINT))
