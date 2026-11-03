"""GitHub check-run/check-suite REST shapes for offline acceptance tests.

Runs carry ``check_suite.id`` as GitHub returns them. By default each suite
has one attempt (its latest view is all of its runs); ``latest`` overrides a
suite's latest-attempt run IDs, ``suites`` the commit's check-suite list.
The commit ``filter=latest`` view is the union of every suite's latest runs.
Per-suite run endpoints are refused: acceptance must not need them.
"""
import re


def pages(key, rows, total=None):
    total = len(rows) if total is None else total
    return [{"total_count": total, key: rows[i:i + 100]} for i in range(0, len(rows), 100)] or \
        [{"total_count": total, key: []}]


def default_suites(runs, sha, latest):
    suites = {}
    for run in runs:
        suites.setdefault(run["check_suite"]["id"], run["app"])
    return [{"id": sid, "app": app, "head_sha": sha, "status": "completed",
             "latest_check_runs_count": len(latest.get(sid, []))} for sid, app in suites.items()]


def check_api(endpoint, runs, sha, *, latest=None, suites=None):
    """The response for a check-run/suite endpoint, or None for any other endpoint."""
    if latest is None:
        latest = {}
        for run in runs:
            latest.setdefault(run["check_suite"]["id"], []).append(run["id"])
    assert not re.search(r"/check-suites/\d+/check-runs", endpoint), endpoint
    if re.search(r"/commits/[0-9a-f]{40}/check-runs\?", endpoint):
        if "filter=all" in endpoint:
            return pages("check_runs", runs)
        assert "filter=latest" in endpoint, endpoint
        ids = {i for suite_ids in latest.values() for i in suite_ids}
        return pages("check_runs", [run for run in runs if run["id"] in ids])
    if re.search(r"/commits/[0-9a-f]{40}/check-suites\?", endpoint):
        return pages("check_suites", default_suites(runs, sha, latest) if suites is None else suites)
    return None
