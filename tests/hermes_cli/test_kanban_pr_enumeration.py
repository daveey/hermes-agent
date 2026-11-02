"""Check-run evidence enumeration, replayed offline from PR #656's exact head.

``fixtures/pr656_check_runs.json`` holds GitHub's recorded responses for head
850d520 (reduced to the fields acceptance reads): 147 runs across 39 suites
(``filter=all``), the commit's ``filter=latest`` view (100 run IDs, in
response order), the commit's 48 check suites, and each suite's
``filter=latest`` run IDs. The required failure 125304345962 belongs to
workflow run 41450628217, whose attempt 1 (47 runs) was superseded by an
attempt 2 that was cancelled before creating any job, so GitHub's latest view
of that suite is empty. No live GitHub or board writes occur here.
"""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from hermes_cli import kanban_pr_acceptance as acceptance
from tests.hermes_cli.check_suite_fake import check_api, pages
from tests.hermes_cli.test_kanban_pr_merge_time import NAME, POST, PRE, SHA

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "pr656_check_runs.json").read_text())
MAIN_SUITE = 112327246670  # push CI on main: attempt 1 failed, attempt 2 empty
PR_SUITE = 112312466991    # pull_request CI: one successful attempt


def recorded():
    runs = [{**dict(zip(RECORDED["run_fields"], row)), "head_sha": SHA, "app": {"id": RECORDED["app_id"]}}
            for row in RECORDED["runs"]]
    for row in runs:
        row["check_suite"] = {"id": row.pop("suite")}
    suites = []
    for row in RECORDED["suites"]:
        suite = dict(zip(RECORDED["suite_fields"], row))
        suite["app"] = {"id": suite.pop("app_id")}
        suites.append(suite)
    latest = {int(k): v for k, v in RECORDED["suite_latest_ids"].items()}
    return runs, suites, latest


def run(check, suite):
    return {**deepcopy(check), "check_suite": {"id": suite}}


@pytest.fixture
def replay(monkeypatch):
    state = {"runs": [run(POST, MAIN_SUITE), run(PRE, PR_SUITE)], "latest": None, "suites": None,
             "state": "OPEN", "merge": SHA, "app": 15368, "branch": "main", "requests": []}

    def api(endpoint, **kwargs):
        state["requests"].append(endpoint)
        if endpoint == "graphql":
            state["query"] = kwargs["query"]
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": SHA, "baseRefName": state["branch"], "state": state["state"],
                "mergedAt": "2026-11-02T13:10:04Z", "mergeCommit": {"oid": state["merge"]},
                "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                    {"context": NAME, "app": {"databaseId": state["app"]}}]}}}}}}
        if "/rules/branches/" in endpoint:
            return [[]]
        if "pages" in state and "/commits/" in endpoint and "/check-runs?" in endpoint:
            return state["pages"]
        if "suite_pages" in state and "/commits/" in endpoint and "/check-suites?" in endpoint:
            return state["suite_pages"]
        checks = check_api(endpoint, state["runs"], SHA, latest=state["latest"], suites=state["suites"])
        if checks is not None:
            return checks
        if "/statuses?" in endpoint:
            return [[]]
        if "/pulls/" in endpoint:
            return {"head": {"sha": SHA}, "base": {"ref": state["branch"]},
                    "state": "closed" if state["state"] == "MERGED" else "open",
                    "merged": state["state"] == "MERGED"}
        pytest.fail(endpoint)

    monkeypatch.setattr(acceptance, "_api", api)
    return state


def collect():
    return acceptance.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")


def check_requests(state):
    return [u for u in state["requests"] if "/check-runs" in u or "/check-suites" in u]


def test_recorded_commit_latest_view_is_each_suites_latest_attempt(replay):
    runs, suites, latest = recorded()
    by_id = {r["id"]: r for r in runs}
    commit_latest = [by_id[i] for i in RECORDED["commit_latest_ids"]]
    # 147 historical runs, 100 latest: exactly the union of every suite's latest
    # attempt, and the 47-run superseded attempt 1 of MAIN_SUITE is not in it.
    assert (len(runs), len(commit_latest), len(suites)) == (147, 100, 48)
    assert set(RECORDED["commit_latest_ids"]) == {i for ids in latest.values() for i in ids}
    assert latest[MAIN_SUITE] == [] and POST["id"] not in RECORDED["commit_latest_ids"]
    assert sum(s["latest_check_runs_count"] for s in suites) == 100
    replay.update(runs=runs, suites=suites, pages=pages("check_runs", commit_latest))
    receipt = collect()
    assert receipt["ok"], receipt
    assert [c["id"] for c in receipt["checks"]] == [PRE["id"]]
    # Two reads, no exhaustive or per-suite enumeration.
    assert check_requests(replay) == [
        f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=latest",
        f"repos/acme/repo/commits/{SHA}/check-suites?per_page=100"]


def test_recorded_failure_retained_while_its_attempt_is_latest(replay):
    # The same 147/39 history before attempt 2: 147 latest runs, past one page.
    runs, suites, latest = recorded()
    latest[MAIN_SUITE] = [r["id"] for r in runs if r["check_suite"]["id"] == MAIN_SUITE]
    for suite in suites:
        if suite["id"] == MAIN_SUITE:
            suite["latest_check_runs_count"] = 47
    replay.update(runs=runs, suites=suites, latest=latest)
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "failure"
    assert {c["id"]: c["classification"] for c in receipt["checks"]} == {
        PRE["id"]: "success", POST["id"]: "failure"}


def test_recorded_superseded_runs_in_latest_view_fail_closed(replay):
    # A latest view still carrying attempt 1 disagrees with the suite's own count of 0.
    runs, suites, _ = recorded()
    replay.update(runs=runs, suites=suites, latest=None)
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "infra"


def test_open_retains_failed_run_of_another_suite(replay):
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "failure"
    assert {c["id"] for c in receipt["checks"]} == {PRE["id"], POST["id"]}


def test_two_same_name_runs_in_one_latest_attempt_are_both_retained(replay):
    # A newest-ID-per-name guess would keep only the newer success.
    failed = run({**PRE, "conclusion": "failure"}, 1)
    passed = run({**PRE, "id": PRE["id"] + 1}, 1)
    replay.update(runs=[failed, passed], latest={1: [failed["id"], passed["id"]]})
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "failure"
    assert sorted(c["id"] for c in receipt["checks"]) == [failed["id"], passed["id"]]


@pytest.mark.parametrize("first,rerun,ok", [("failure", "success", True), ("success", "failure", False)])
def test_rerun_attempt_supersedes_the_earlier_attempt(replay, first, rerun, ok):
    old = run({**PRE, "conclusion": first}, 1)
    new = run({**POST, "conclusion": rerun}, 1)
    replay.update(runs=[old, new], latest={1: [new["id"]]})
    receipt = collect()
    assert receipt["ok"] is ok
    assert [c["id"] for c in receipt["checks"]] == [POST["id"]]


def _big_suite(state):
    filler = [run({**PRE, "id": i, "name": f"optional {i}"}, 1) for i in range(1, 151)]
    state["runs"] = filler + [run(PRE, 1)]
    return filler


def test_latest_view_past_one_page_is_paginated(replay):
    _big_suite(replay)
    assert len(check_api(f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=latest",
                         replay["runs"], SHA) or []) == 2
    assert collect()["ok"]


def test_latest_view_capped_below_suite_count_fails_closed(replay):
    # A self-consistent count is not completeness: the suite says 151 latest runs.
    filler = _big_suite(replay)
    replay["pages"] = [{"total_count": 100, "check_runs": filler[:100]}]
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "infra"


def _suite(**changes):
    return {"id": 1, "app": {"id": 15368}, "head_sha": SHA, "latest_check_runs_count": 1, **changes}


def _many_suites(n):
    return [_suite()] + [_suite(id=i, latest_check_runs_count=0) for i in range(2, n + 1)]


FAULTS = {
    # latest view pagination and identity
    "short": lambda r, s, l: l[0].update(total_count=2),
    "changed_total": lambda r, s, l: l.append({"total_count": 2, "check_runs": []}),
    "duplicate": lambda r, s, l: l[0]["check_runs"].append(r),
    "no_pages": lambda r, s, l: l.clear(),
    "bad_suite": lambda r, s, l: r.update(check_suite={"id": None}),
    "no_suite": lambda r, s, l: r.pop("check_suite"),
    "bad_id": lambda r, s, l: r.update(id=None),
    "no_app": lambda r, s, l: r.pop("app"),
    # check-suite list
    "suites_short": lambda r, s, l: s[0].update(total_count=2),
    "no_suite_pages": lambda r, s, l: s.clear(),
    "bad_suite_id": lambda r, s, l: s[0]["check_suites"][0].update(id=0),
    "orphan_run": lambda r, s, l: s[0]["check_suites"][0].update(id=2),
    "suite_head": lambda r, s, l: s[0]["check_suites"][0].update(head_sha="b" * 40),
    "suite_app": lambda r, s, l: s[0]["check_suites"][0].update(app={"id": 99}),
    "bad_latest_count": lambda r, s, l: s[0]["check_suites"][0].update(latest_check_runs_count=None),
    "negative_latest_count": lambda r, s, l: s[0]["check_suites"][0].update(latest_check_runs_count=-1),
    "too_many_suites": lambda r, s, l: s.__setitem__(slice(None), pages("check_suites", _many_suites(1001))),
    # per-suite tally
    "count_above_tally": lambda r, s, l: s[0]["check_suites"][0].update(latest_check_runs_count=2),
    "zero_count_with_runs": lambda r, s, l: s[0]["check_suites"][0].update(latest_check_runs_count=0),
    "absent_suite_with_count": lambda r, s, l: s[0].update(
        total_count=2, check_suites=[*s[0]["check_suites"], _suite(id=2)]),
}


def _evidence(state, fault=None):
    good = run(PRE, 1)
    suite_pages = pages("check_suites", [_suite()])
    latest_pages = [{"total_count": 1, "check_runs": [good]}]
    if fault:
        FAULTS[fault](good, suite_pages, latest_pages)
    state.update(runs=[good], pages=latest_pages, suite_pages=suite_pages)


def test_uncorrupted_control_passes(replay):
    _evidence(replay)
    assert collect()["ok"]


@pytest.mark.parametrize("n", [2, 1000])
def test_absent_zero_count_suites_pass_up_to_the_suite_limit(replay, n):
    _evidence(replay)
    replay["suite_pages"] = pages("check_suites", _many_suites(n))
    assert collect()["ok"]


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_incomplete_malformed_or_racing_evidence_fails_closed(replay, fault):
    _evidence(replay, fault)
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "infra", fault


@pytest.mark.parametrize("fault", ["other_app", "stale_head", "queued_rerun"])
def test_latest_selection_cannot_hide_strict_evidence(replay, fault):
    old = run(PRE, 1)
    new = run(POST, 1)
    latest = None
    if fault == "other_app":
        # Another app's run is its own suite and stays visible to an unpinned context.
        replay["app"] = None
        new.update(app={"id": 99}, check_suite={"id": 2})
    elif fault == "stale_head":
        new.update(head_sha="b" * 40, conclusion="success")
    else:
        new.update(status="queued", conclusion=None, started_at=None, completed_at=None)
        latest = {1: [new["id"]]}
    replay.update(runs=[old, new], latest=latest)
    assert not collect()["ok"]


@pytest.mark.parametrize("landed", [SHA, "b" * 40, None])
def test_main_health_requires_proven_landed_head(replay, landed):
    replay.update(state="MERGED", merge=landed)
    receipt = collect()
    assert receipt["ok"]
    assert receipt["post_merge_checks"][0]["classification"] == "failure"
    assert ("main_health" in receipt) is (landed == SHA)
    assert "mergeCommit" in replay["query"]
    # MERGED keeps the exhaustive history (cut at merge), not the latest view.
    assert check_requests(replay) == [f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=all"]


def test_same_second_red_start_is_ambiguous_not_postmerge(replay):
    replay.update(state="MERGED")
    replay["runs"][0]["started_at"] = "2026-11-02T13:10:04Z"
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "pending"
    assert not receipt["post_merge_checks"]
