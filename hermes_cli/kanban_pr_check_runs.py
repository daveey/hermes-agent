"""Complete check-run enumeration and proof of GitHub's ``filter=latest`` view.

For an OPEN PR the evidence is GitHub's commit ``filter=latest`` view, which is
defined per check suite: the runs of the suite's latest attempt. A rerun
supersedes the whole earlier attempt (even while the new attempt has no runs),
and two same-name runs of one attempt are both latest. That is not derivable
from run IDs or names, so the view is taken from GitHub as is and proven
complete against the commit's check suites: every latest run belongs to a
listed suite of the same app and head, and each suite's latest tally equals its
own ``latest_check_runs_count`` (zero and absent included). Anything
inconsistent (truncation, malformed identity, a race between the two reads)
raises ``ValueError``: retry, never guess.
"""

_SUITE_LIMIT = 1000  # commits/{ref}/check-runs only covers the 1000 newest suites


def _positive_id(value, what):
    if type(value) is not int or value <= 0:
        raise ValueError(f"Invalid {what} ID")
    return value


def _complete(pages, key, what):
    """Every page agrees on one count and the pages hold exactly that many unique IDs."""
    if not isinstance(pages, list) or not pages:
        raise ValueError(f"Missing {what} pages")
    total = pages[0]["total_count"]
    if type(total) is not int or total < 0:
        raise ValueError(f"Invalid {what} count")
    rows = []
    for page in pages:
        if type(page["total_count"]) is not int or page["total_count"] != total:
            raise ValueError(f"The {what} count changed during pagination")
        if not isinstance(page[key], list):
            raise ValueError(f"Invalid {what} page")
        rows.extend(page[key])
    ids = [_positive_id(row["id"], what) for row in rows]
    if len(rows) != total or len(set(ids)) != total:
        raise ValueError(f"Incomplete {what} pagination")
    return rows


def complete_runs(pages):
    """A filtered endpoint's self-consistent count is not a completeness proof."""
    return _complete(pages, "check_runs", "check-run")


def _suite_of(run):
    suite = run.get("check_suite")
    if not isinstance(suite, dict):
        raise ValueError("Check run has no check-suite identity")
    return _positive_id(suite.get("id"), "check-suite")


def _app_of(row):
    app = row.get("app")
    if not isinstance(app, dict):
        raise ValueError("Missing app identity")
    return _positive_id(app.get("id"), "app")


def proven_latest_runs(latest_pages, suite_pages, sha):
    """GitHub's commit ``filter=latest`` runs, proven complete suite by suite.

    ``latest_pages`` is the paginated ``commits/{sha}/check-runs?filter=latest``
    response, ``suite_pages`` the paginated ``commits/{sha}/check-suites``
    response. Returns the latest runs unchanged.
    """
    runs = complete_runs(latest_pages)
    suites = _complete(suite_pages, "check_suites", "check-suite")
    if len(suites) > _SUITE_LIMIT:
        raise ValueError("Too many check suites for complete check-run enumeration")
    expected = {}
    for suite in suites:
        count = suite.get("latest_check_runs_count")
        if type(count) is not int or count < 0:
            raise ValueError("Invalid latest check-run count")
        if suite.get("head_sha") != sha:
            raise ValueError("Check suite belongs to another head")
        expected[suite["id"]] = (_app_of(suite), count)
    tally = {}
    for run in runs:
        suite_id = _suite_of(run)
        if suite_id not in expected:
            raise ValueError("Check run outside the commit's check suites")
        if _app_of(run) != expected[suite_id][0]:
            raise ValueError("Check run app differs from its suite")
        tally[suite_id] = tally.get(suite_id, 0) + 1
    for suite_id, (_, count) in expected.items():
        # Zero too: a suite whose latest attempt has no runs must contribute none.
        if tally.get(suite_id, 0) != count:
            raise ValueError("Latest check runs disagree with their suite's latest count")
    return runs
