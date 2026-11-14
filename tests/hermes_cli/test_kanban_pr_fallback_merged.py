"""Merged-path fail-opens in the no-required-check fallback (review of PR #2830).

1. A completed 0-run suite that failed (workflow broke before creating jobs)
   must be red on the MERGED path too, not only OPEN.
2. A later same-name run in the same suite supersedes an earlier failure only
   when Actions proves it is a rerun (same workflow run, higher run_attempt,
   one such job per attempt); a later sibling job never does.
"""
import pytest

from hermes_cli import kanban_pr_acceptance as gate
from tests.hermes_cli import test_kanban_pr_fallback as base

evidence = base.evidence
URL, SHA, run, suite, rerun_pair, as_actions = (
    base.URL, base.SHA, base.run, base.suite, base.rerun_pair, base.as_actions)
PRE_MERGE = "2026-11-02T12:00:00Z"   # merge is 13:00
POST_MERGE = "2026-11-02T13:30:00Z"


def failed_suite(conclusion="failure", *, created=PRE_MERGE, app=base.ACTIONS):
    return {**suite(100, app, status="completed", created=created), "conclusion": conclusion}


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required",
                                        "startup_failure", "stale", None])
@pytest.mark.parametrize("merged", [False, True])
def test_failed_empty_suite_is_red_on_both_paths(evidence, conclusion, merged):
    evidence.update(merged=merged, runs=[run("success")], extra_suites=[failed_suite(conclusion)])
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == "failure"
    assert receipt["ok"] is False
    assert receipt["checks"][-1]["id"] == 100
    assert receipt["checks"][-1]["conclusion"] == conclusion


@pytest.mark.parametrize("merged", [False, True])
def test_failed_empty_suite_alone_is_not_waivable_absence(evidence, merged):
    evidence.update(merged=merged, extra_suites=[failed_suite()])
    assert gate.collect_acceptance(URL, None)["classification"] == "failure"


def test_failed_empty_suite_after_merge_is_post_merge_evidence(evidence):
    # A main-push workflow on the merged sha belongs to ci-main-watch.
    evidence.update(merged=True, runs=[run("success")], extra_suites=[failed_suite(created=POST_MERGE)])
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == "success"
    assert [c["id"] for c in receipt["post_merge_checks"]] == [100]


@pytest.mark.parametrize("created", [None, "not a time", "2026-11-02T13:00:00Z"])
def test_failed_empty_suite_unprovable_or_tied_age_fails_closed(evidence, created):
    evidence.update(merged=True, runs=[run("success")], extra_suites=[failed_suite(created=created)])
    assert gate.collect_acceptance(URL, None)["classification"] == "failure"


@pytest.mark.parametrize("conclusion", ["success", "neutral", "skipped"])
@pytest.mark.parametrize("merged,expected", [(False, "missing"), (True, "success")])
def test_passing_empty_suite_keeps_prior_semantics(evidence, conclusion, merged, expected):
    evidence.update(merged=merged, runs=[run("success")], extra_suites=[failed_suite(conclusion)])
    assert gate.collect_acceptance(URL, None)["classification"] == expected


def test_later_same_name_without_rerun_proof_keeps_failure(evidence):
    # Review probe: failure ended 12:10, same-suite success started 12:20.
    evidence.update(merged=True, runs=rerun_pair("failure", second_start="2026-11-02T12:20:00Z"))
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == "failure"
    assert [c["id"] for c in receipt["checks"]] == [1, 2]


def test_sequential_matrix_sibling_is_not_a_rerun(evidence):
    # max-parallel: 1 matrix with a static job name: same workflow run AND attempt.
    runs = as_actions(evidence, rerun_pair("failure", second_start="2026-11-02T12:20:00Z"), [1, 1])
    evidence.update(merged=True, runs=runs)
    assert gate.collect_acceptance(URL, None)["classification"] == "failure"


def test_rerun_of_one_sibling_cannot_hide_the_others_failure(evidence):
    # Attempt 1 ran two same-name jobs; attempt 2 re-ran one. Which one is unprovable.
    a1_fail, a1_pass = rerun_pair("failure", second_start="2026-11-02T12:20:00Z")
    a1_pass["completed_at"] = "2026-11-02T12:25:00Z"
    a2 = run("success", id=3, started="2026-11-02T12:40:00Z")
    evidence.update(merged=True, runs=as_actions(evidence, [a1_fail, a1_pass, a2], [1, 1, 2]))
    assert gate.collect_acceptance(URL, None)["classification"] == "failure"


@pytest.mark.parametrize("tamper", [
    {"run_id": 100},                                                 # another workflow run
    {"run_attempt": 1},                                              # same attempt
    {"run_attempt": 0},                                              # an earlier attempt
    {"run_attempt": "2"},                                            # unreadable attempt
    {"check_run_url": "https://api.github.com/repos/acme/repo/check-runs/9"},  # another job
    {"head_sha": "b" * 40},                                          # another commit
    None,                                                            # job unreadable (404)
])
def test_rerun_identity_must_be_proven(evidence, tamper):
    runs = as_actions(evidence, rerun_pair("failure"), [1, 2])
    if tamper is None:
        del evidence["jobs"][2]
    else:
        evidence["jobs"][2].update(tamper)
    evidence.update(merged=True, runs=runs)
    assert gate.collect_acceptance(URL, None)["classification"] == "failure"


def test_non_actions_app_rerun_is_unprovable(evidence):
    runs = as_actions(evidence, rerun_pair("failure"), [1, 2])
    for check in runs:
        check["app"] = {"id": 1}
    evidence.update(merged=True, runs=runs)
    assert gate.collect_acceptance(URL, None)["classification"] == "failure"


def test_proven_rerun_chain_supersedes_each_failed_attempt(evidence):
    runs = [{**run("failure"), "completed_at": "2026-11-02T12:05:00Z"},
            {**run("failure", id=2, started="2026-11-02T12:10:00Z"), "completed_at": "2026-11-02T12:15:00Z"},
            run("success", id=3, started="2026-11-02T12:20:00Z")]
    evidence.update(merged=True, runs=as_actions(evidence, runs, [1, 2, 3]))
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == "success"
    assert [c["id"] for c in receipt["checks"]] == [3]


def test_post_merge_sibling_in_rerun_attempt_blocks_proof(evidence):
    # Attempt 2 holds two same-name jobs once its post-merge one counts.
    runs = rerun_pair("failure") + [run("success", id=3, started=POST_MERGE)]
    evidence.update(merged=True, runs=as_actions(evidence, runs, [1, 2, 2]))
    assert gate.collect_acceptance(URL, None)["classification"] == "failure"


def test_unreadable_same_name_job_blocks_proof(evidence):
    # A same-name job of unknown attempt may be a sibling of either run.
    runs = rerun_pair("failure") + [run("success", id=3, started=POST_MERGE)]
    evidence.update(merged=True, runs=as_actions(evidence, runs, [1, 2, 3]))
    del evidence["jobs"][3]
    assert gate.collect_acceptance(URL, None)["classification"] == "failure"


def test_required_path_never_reads_actions_jobs(evidence):
    from hermes_cli import kanban_pr_merge_time as mt

    runs = as_actions(evidence, rerun_pair("failure"), [1, 2])
    receipt = {"head_sha": SHA}
    pr = {"state": "MERGED", "mergedAt": "2026-11-02T13:00:00Z", "baseRefName": "main"}
    kept, _ = mt.merge_evidence(receipt, pr, "acme/repo", runs, [], distinct_suites=False,
                                attempt_of=lambda c: pytest.fail("required path proved a rerun"))
    assert [c["id"] for c in kept] == [2]  # required semantics: latest per (name, app)
