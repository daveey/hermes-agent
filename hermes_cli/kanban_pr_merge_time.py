"""Separate immutable PR acceptance evidence from later main-health checks."""
from __future__ import annotations

from datetime import datetime


def timestamp(value):
    """Require an unambiguous GitHub timestamp; unknown chronology fails closed."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("GitHub timestamp has no timezone")
    return parsed


def check_receipt(check, sha):
    from hermes_cli.kanban_pr_acceptance import _classify

    is_run = "conclusion" in check
    outcome = check.get("conclusion") if is_run else check["state"]
    return {"name": check.get("name", check.get("context")), "id": check["id"],
            "url": check.get("html_url") or check.get("target_url"),
            # Retention deletes GitHub runs too. Keep audit rows in pr_acceptance
            # events, including chronology, rather than only an expiring URL.
            "html_url": check.get("html_url") or check.get("target_url"),
            "completed_at": check.get("completed_at") if is_run else check.get("created_at"),
            "head_sha": check.get("head_sha", check.get("sha")),
            "classification": _classify(check, sha, outcome, is_run), "conclusion": outcome}


def partition_at_merge(checks, merged_at, *, runs, distinct_suites=False, attempt_of=None):
    """Latest pre-merge execution per producer, plus every later execution.

    App identities stay distinct: an unpinned context must not hide another app's
    failed run behind a success. Legacy statuses key on context + creator, so a
    later status by someone else never hides the pinned app's own status.
    """
    cutoff = timestamp(merged_at)

    def start(check):
        return timestamp(check.get("started_at") if runs else check.get("created_at"))

    def key_of(check):
        if runs:
            if distinct_suites:
                # Workflow suites stay distinct; supersession inside one is
                # proven by chronology below, never assumed from the name.
                return (check["name"], check["app"]["id"], check["check_suite"]["id"])
            return (check["name"], check["app"]["id"])
        return (check["context"], (check.get("creator") or {}).get("id"))

    def concluded_at_merge(check):
        done = check.get("completed_at")
        return check.get("status") == "completed" and bool(done) and timestamp(done) <= cutoff

    # mergedAt has 1 s resolution, so a run started in the merge's own second is
    # unordered against it. If it had not concluded by then while an earlier same-key
    # run had, that earlier conclusion is what gated the merge: the tie run is later
    # (post-merge) evidence. With no earlier conclusion it stays pending (fail closed).
    # Same-name runs in one suite may be concurrent siblings, so in the fallback
    # a tie run is never assumed to follow another run's conclusion.
    def gate_key(check):
        return (*key_of(check), check["id"]) if runs and distinct_suites else key_of(check)

    gated = {gate_key(c) for c in checks if start(c) < cutoff and concluded_at_merge(c)} if runs else set()
    latest = {}
    post = []
    for check in checks:
        started = start(check)
        tie_after_gate = (runs and started == cutoff and not concluded_at_merge(check)
                          and gate_key(check) in gated)
        if started > cutoff or tie_after_gate:
            post.append(check)
            continue
        # A pre-merge start does not make its later conclusion evidence at merge.
        if runs and check.get("completed_at") and timestamp(check["completed_at"]) > cutoff:
            check = {**check, "status": "in_progress", "conclusion": None}
        key = key_of(check)
        if runs and distinct_suites:
            latest.setdefault(key, []).append(check)
            continue
        order = (started, check["id"])
        if key not in latest or order > latest[key][0]:
            latest[key] = (order, check)
    if runs and distinct_suites:
        family = {}
        for check in checks:
            family.setdefault(key_of(check), []).append(check)
        return [c for key, group in latest.items()
                for c in _unsuperseded(group, start, family[key], attempt_of)], post
    return [entry[1] for entry in latest.values()], post


def _unsuperseded(group, start, family, attempt_of):
    """Drop a same-name, same-suite run only when a proven rerun of it followed.

    Chronology alone cannot tell a rerun from a later same-named sibling job in
    the suite (a max-parallel:1 matrix with a static job name). A rerun is proven
    only by its Actions identity: the same workflow run at a higher run_attempt,
    each attempt holding exactly one job of this name (``family`` is every
    same-key run, pre- and post-merge). Overlapping, tied, never-completed and
    unprovable runs all stay as evidence (fail closed).
    """
    def ended(check):
        done = check.get("completed_at")
        return timestamp(done) if check.get("status") == "completed" and done else None

    cache = {}

    def attempt(check):
        if check["id"] not in cache:
            cache[check["id"]] = attempt_of(check) if attempt_of else None
        return cache[check["id"]]

    def attempts_unique():
        seen = [attempt(c) for c in family]
        return None not in seen and len(seen) == len(set(seen))

    kept = []
    for check in group:
        end = ended(check)
        later = [c for c in group if end is not None and start(c) > end]
        mine = attempt(check) if later else None
        if mine and attempts_unique() and any(
                (other := attempt(c)) and other[0] == mine[0] and other[1] > mine[1] for c in later):
            continue
        kept.append(check)
    return kept


def job_attempt(job, check):
    """(workflow run id, run_attempt) of an Actions job, only if it is this check run."""
    url, run_id, run_attempt = job.get("check_run_url"), job.get("run_id"), job.get("run_attempt")
    if not (isinstance(url, str) and url.endswith(f"/check-runs/{check['id']}")
            and job.get("head_sha") == check.get("head_sha")):
        return None
    if any(type(v) is not int for v in (run_id, run_attempt)):
        return None
    return run_id, run_attempt


def merge_evidence(receipt, pr, repo, runs, statuses, *, distinct_suites=False, attempt_of=None):
    if pr["state"] != "MERGED":
        return runs, statuses
    merged_at = pr["mergedAt"]
    receipt["merged_at"] = merged_at
    runs, post_runs = partition_at_merge(runs, merged_at, runs=True, distinct_suites=distinct_suites,
                                         attempt_of=attempt_of)
    statuses, post_statuses = partition_at_merge(statuses, merged_at, runs=False)
    sha = receipt["head_sha"]
    receipt["post_merge_checks"] = [check_receipt(c, sha) for c in post_runs + post_statuses]
    flag_main_health(receipt, pr, repo)
    return runs, statuses


# Post-merge conclusions that mean the landed sha's CI is red. startup_failure is
# a workflow that could not start (e.g. an invalid workflow file): red. cancelled
# is not: concurrency groups routinely cancel a superseded main run. stale,
# neutral and skipped never claim red either.
RED_CONCLUSIONS = frozenset({"failure", "error", "timed_out", "action_required", "startup_failure"})


def flag_main_health(receipt, pr, repo):
    """Point at ci-main-watch when post-merge evidence on the landed sha is red.

    Callers that add post-merge rows later (the fallback's empty suites) call it
    again; it only ever adds the pointer, so a second call is idempotent.
    """
    sha = receipt["head_sha"]
    landed = pr.get("mergeCommit")
    if (isinstance(landed, dict) and landed.get("oid") == sha
            and any(c.get("conclusion") in RED_CONCLUSIONS for c in receipt["post_merge_checks"])):
        branch = pr["baseRefName"]
        label = repo if branch == "main" else f"{repo}@{branch}"
        receipt["main_health"] = {
            "idempotency_key": f"ci-red:{label}:{sha}",
            "detail": f"Base branch {branch} CI is red for {repo}@{sha}; "
                      "ci-main-watch owns base health, not this PR's acceptance."}


def _health_owner_rows(conn, exact_key):
    # ci-main-watch keys a continuing episode by FIRST red SHA, not current SHA.
    # Off main its producer label is repo@branch. Match the whole label, not LIKE
    # (repo/branch names may contain SQL wildcard characters).
    prefix = exact_key.rsplit(":", 1)[0] + ":"
    return conn.execute(
        "SELECT id, status, idempotency_key, created_at FROM tasks "
        "WHERE created_by='ci-main-watch' "
        "AND status IN ('triage','todo','scheduled','ready','running','blocked','review') "
        "AND substr(idempotency_key,1,?)=? "
        "ORDER BY created_at DESC, id DESC", (len(prefix), prefix)).fetchall()


def annotate_main_health_owner(conn, receipt):
    """Diagnostic only: discover existing owners across boards without creating any.

    Other board handles are mode=ro (never the initializing/migrating connect()).
    Missing, old-schema or unreadable boards cannot change acceptance or prevent
    its receipt from being persisted. No profile switching or global mutations.
    """
    import re
    import sqlite3
    from hermes_cli import kanban_lockguard
    from hermes_cli.kanban_db_workspace import _sibling_board_db_files

    health = receipt.get("main_health")
    if not health:
        return
    key = health["idempotency_key"]
    rows = []
    try:
        rows.extend(_health_owner_rows(conn, key))
        paths = _sibling_board_db_files(conn)
    except (OSError, sqlite3.Error):
        paths = []
    for path in paths:
        try:
            other = kanban_lockguard.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
            try:
                other.row_factory = sqlite3.Row
                rows.extend(_health_owner_rows(other, key))
            finally:
                other.close()
        except (OSError, sqlite3.Error):
            continue
    rows = [row for row in rows if re.fullmatch(r"[0-9a-f]{40}", row["idempotency_key"].rsplit(":", 1)[-1])]
    if not rows:
        return
    row = max(rows, key=lambda r: (r["idempotency_key"] == key, r["created_at"], r["id"]))
    health["owner_card"] = row["id"]
    health["owner_status"] = row["status"]
    health["detail"] += f" Main-health owner card: {row['id']} ({row['status']})."
