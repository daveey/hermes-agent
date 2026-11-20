"""A concurrency-cancelled run is superseded by a later green run of the same workflow.

No-required-check fallback, MERGED path (live: PR #118, run 44681460700 cancelled by the
label event that started 44681568995). Pairing is proven from Actions identity (workflow
id, head SHA, event), never from the job name alone; everything else stays fail closed.
"""
import pytest

from hermes_cli import kanban_pr_acceptance as gate
from tests.hermes_cli import test_kanban_pr_fallback as base

evidence = base.evidence
URL, SHA = base.URL, base.SHA
ACTIONS = base.ACTIONS
EARLY, LATE = 100, 200  # workflow run ids
MERGE = "2026-11-02T13:00:00Z"
POST_MERGE = "2026-11-02T13:30:00Z"


def job(check_id, run_id, attempt=1, sha=SHA):
    return {"run_id": run_id, "run_attempt": attempt, "head_sha": sha,
            "check_run_url": f"https://api.github.com/repos/acme/repo/check-runs/{check_id}"}


def workflow_run(run_id, *, created, conclusion="success", **fields):
    return {"id": run_id, "workflow_id": 5, "event": "pull_request", "head_sha": SHA,
            "created_at": created, "updated_at": "2026-11-02T12:30:00Z", "status": "completed",
            "conclusion": conclusion, **fields}


def check(outcome, check_id, suite_id, *, started, completed, app=ACTIONS, name="check"):
    return {"id": check_id, "name": name, "head_sha": SHA, "app": {"id": app}, "check_suite": {"id": suite_id},
            "started_at": started, "completed_at": completed, "status": "completed", "conclusion": outcome}


@pytest.fixture
def actions(evidence, monkeypatch):
    """The PR #118 shape: a cancelled earlier run, then a green run of the same workflow."""
    state = {"workflow_runs": {
        EARLY: workflow_run(EARLY, created="2026-11-02T11:00:00Z", conclusion="cancelled"),
        LATE: workflow_run(LATE, created="2026-11-02T11:01:00Z"),
    }, "calls": []}
    evidence["jobs"] = {1: job(1, EARLY), 2: job(2, LATE)}
    evidence["runs"] = [
        check("cancelled", 1, 7, started="2026-11-02T11:00:30Z", completed="2026-11-02T11:02:00Z"),
        check("success", 2, 8, started="2026-11-02T11:01:30Z", completed="2026-11-02T12:30:00Z"),
    ]
    evidence["merged"] = True
    inner = gate._api

    def api(endpoint, **kwargs):
        if "/actions/runs/" in endpoint:
            state["calls"].append(endpoint)
            found = state["workflow_runs"].get(int(endpoint.rsplit("/", 1)[1]))
            if found is None:
                raise gate._GateAuthError("HTTP 404 on " + endpoint)
            return found
        return inner(endpoint, **kwargs)

    monkeypatch.setattr(gate, "_api", api)
    return state


def collect():
    return gate.collect_acceptance(URL, None)


def ids(receipt):
    return [c["id"] for c in receipt["checks"]]


def test_cancelled_run_superseded_by_later_green_workflow_run(evidence, actions):
    receipt = collect()
    assert receipt["classification"] == "success"
    assert receipt["ok"] is True
    assert ids(receipt) == [2]


@pytest.mark.parametrize("tamper", [
    {"completed_at": None},
    {"status": "in_progress"},
    {"completed_at": POST_MERGE},
])
def test_cancelled_job_requires_terminal_premerge_completion(evidence, actions, tamper):
    evidence["runs"][0].update(tamper)
    receipt = collect()
    assert receipt["ok"] is False
    assert 1 in ids(receipt)


def test_chain_of_cancelled_runs_each_superseded(evidence, actions):
    actions["workflow_runs"][50] = workflow_run(50, created="2026-11-02T10:00:00Z", conclusion="cancelled")
    evidence["runs"].insert(0, check("cancelled", 3, 6, started="2026-11-02T10:00:30Z",
                                      completed="2026-11-02T10:01:00Z"))
    evidence["jobs"][3] = job(3, 50)
    assert ids(collect()) == [2]


def test_earlier_failure_is_never_superseded(evidence, actions):
    evidence["runs"][0]["conclusion"] = "failure"
    receipt = collect()
    assert receipt["classification"] == "failure"
    assert ids(receipt) == [1, 2]


@pytest.mark.parametrize("conclusion", ["timed_out", "action_required", "stale", "startup_failure"])
def test_only_cancelled_is_superseded(evidence, actions, conclusion):
    evidence["runs"][0]["conclusion"] = conclusion
    assert collect()["classification"] == "infra"


def test_no_later_run_keeps_cancelled_infra(evidence, actions):
    del evidence["runs"][1]
    receipt = collect()
    assert receipt["classification"] == "infra"
    assert ids(receipt) == [1]


@pytest.mark.parametrize("outcome", ["failure", "cancelled"])
def test_later_run_not_success_stays_red(evidence, actions, outcome):
    evidence["runs"][1]["conclusion"] = outcome
    receipt = collect()
    assert receipt["classification"] == "infra"  # the cancelled row is first and stays
    assert ids(receipt) == [1, 2]


def test_later_run_still_pending_stays_pending(evidence, actions):
    evidence["runs"][1].update(status="in_progress", conclusion=None, completed_at=None)
    receipt = collect()
    assert receipt["classification"] == "infra"  # cancelled row first; pending row stays too
    assert {c["classification"] for c in receipt["checks"]} == {"infra", "pending"}


def test_later_job_concluding_after_merge_does_not_supersede(evidence, actions):
    evidence["runs"][1]["completed_at"] = POST_MERGE
    receipt = collect()
    assert receipt["ok"] is False
    assert 1 in ids(receipt)


def test_later_job_started_after_merge_does_not_supersede(evidence, actions):
    evidence["runs"][1].update(started_at=POST_MERGE, completed_at=POST_MERGE)
    receipt = collect()
    assert receipt["classification"] == "infra"
    assert ids(receipt) == [1]


def test_later_workflow_run_concluding_after_merge_does_not_supersede(evidence, actions):
    # The job was green before the merge but the workflow run was touched afterwards.
    actions["workflow_runs"][LATE]["updated_at"] = POST_MERGE
    receipt = collect()
    assert receipt["classification"] == "infra"
    assert 1 in ids(receipt)


@pytest.mark.parametrize("tamper", [{"status": "in_progress"}, {"conclusion": "failure"}, {"conclusion": None}])
def test_later_workflow_run_must_be_a_completed_success(evidence, actions, tamper):
    actions["workflow_runs"][LATE].update(tamper)
    assert 1 in ids(collect())


@pytest.mark.parametrize("tamper", [
    {"workflow_id": 6},                       # another workflow
    {"event": "push"},                        # another event
    {"head_sha": "b" * 40},                   # another commit
    {"created_at": "2026-11-02T10:00:00Z"},   # not later than the cancelled run
    {"created_at": "2026-11-02T11:00:00Z"},   # tied
    {"id": 1},                                # run id does not match the job's run
    {"workflow_id": "5"},                     # unreadable identity
    {"event": ""},
    {"created_at": "yesterday"},
])
def test_later_run_identity_must_be_proven(evidence, actions, tamper):
    actions["workflow_runs"][LATE].update(tamper)
    receipt = collect()
    assert receipt["classification"] == "infra"
    assert 1 in ids(receipt)


@pytest.mark.parametrize("tamper", [{"workflow_id": 6}, {"event": "push"}, {"head_sha": "b" * 40}])
def test_cancelled_run_identity_must_be_proven(evidence, actions, tamper):
    actions["workflow_runs"][EARLY].update(tamper)
    assert collect()["classification"] == "infra"


def test_same_workflow_run_is_not_another_run(evidence, actions):
    # Both jobs inside one workflow run: a sibling, not a later run.
    evidence["jobs"][2]["run_id"] = EARLY
    assert collect()["classification"] == "infra"


@pytest.mark.parametrize("which", [0, 1])
def test_non_actions_producer_never_pairs(evidence, actions, which):
    evidence["runs"][which]["app"] = {"id": 1}
    receipt = collect()
    assert receipt["classification"] == "infra"
    assert 1 in ids(receipt)
    if which == 0:
        assert actions["calls"] == []


def test_job_name_alone_is_not_proof(evidence, actions):
    evidence["runs"][1]["name"] = "other"
    assert 1 in ids(collect())


@pytest.mark.parametrize("fault", ["job_404", "job_other_check", "job_other_head", "run_404"])
def test_unreadable_actions_identity_fails_closed(evidence, actions, fault):
    if fault == "job_404":
        del evidence["jobs"][2]
    elif fault == "job_other_check":
        evidence["jobs"][2]["check_run_url"] = "https://api.github.com/repos/acme/repo/check-runs/9"
    elif fault == "job_other_head":
        evidence["jobs"][2]["head_sha"] = "b" * 40
    else:
        del actions["workflow_runs"][LATE]
    receipt = collect()
    assert receipt["classification"] == "infra"
    assert 1 in ids(receipt)


def test_sibling_same_name_job_in_later_run_blocks_proof(evidence, actions):
    # The later workflow run holds two jobs named like the cancelled one: which one gates is unprovable.
    evidence["runs"].append(check("failure", 3, 8, started="2026-11-02T11:01:30Z", completed="2026-11-02T12:00:00Z"))
    evidence["jobs"][3] = job(3, LATE)
    receipt = collect()
    assert receipt["classification"] == "infra"
    assert 1 in ids(receipt)


def test_sibling_same_name_job_in_cancelled_run_blocks_proof(evidence, actions):
    evidence["runs"].append(check("success", 3, 7, started="2026-11-02T11:00:30Z", completed="2026-11-02T11:02:00Z"))
    evidence["jobs"][3] = job(3, EARLY)
    receipt = collect()
    assert receipt["classification"] == "infra"
    assert 1 in ids(receipt)


def test_post_merge_same_name_job_in_later_workflow_blocks_proof(evidence, actions):
    evidence["runs"].append(check("success", 3, 8, started=POST_MERGE, completed=POST_MERGE))
    evidence["jobs"][3] = job(3, LATE)
    assert 1 in ids(collect())


def test_open_pr_is_unchanged(evidence, actions):
    # OPEN reads GitHub's latest view; no workflow-run lookups, cancelled stays infra.
    evidence.update(merged=False, runs=evidence["runs"][:1])
    receipt = collect()
    assert receipt["classification"] == "infra"
    assert actions["calls"] == []


def test_required_path_never_reads_workflow_runs():
    from hermes_cli import kanban_pr_merge_time as mt

    runs = [check("cancelled", 1, 7, started="2026-11-02T11:00:30Z", completed="2026-11-02T11:02:00Z"),
            check("success", 2, 8, started="2026-11-02T11:01:30Z", completed="2026-11-02T12:30:00Z")]
    pr = {"state": "MERGED", "mergedAt": MERGE, "baseRefName": "main"}
    kept, _ = mt.merge_evidence({"head_sha": SHA}, pr, "acme/repo", runs, [], distinct_suites=False,
                                run_of=lambda c: pytest.fail("required path read a workflow run"))
    assert [c["id"] for c in kept] == [2]


def test_later_job_started_in_the_merge_second_does_not_supersede(evidence, actions):
    # mergedAt has 1 s resolution: a start tied with the merge is not "before" it.
    evidence["runs"][1].update(started_at=MERGE, completed_at=MERGE)
    assert 1 in ids(collect())


def test_unresolvable_same_name_job_blocks_proof(evidence, actions):
    # A third same-name job whose Actions identity is unreadable may belong to either run.
    evidence["runs"].append(check("success", 3, 9, started="2026-11-02T12:00:00Z", completed="2026-11-02T12:10:00Z"))
    assert 1 in ids(collect())


@pytest.mark.parametrize("field,value", [("head_sha", "b" * 40), ("event", "push"), ("workflow_id", 6)])
def test_partition_compares_every_identity_field(field, value):
    from hermes_cli import kanban_pr_merge_time as mt

    runs = [check("cancelled", 1, 7, started="2026-11-02T11:00:30Z", completed="2026-11-02T11:02:00Z"),
            check("success", 2, 8, started="2026-11-02T11:01:30Z", completed="2026-11-02T12:30:00Z")]
    identity = {1: workflow_run(EARLY, created="2026-11-02T11:00:00Z"),
                2: workflow_run(LATE, created="2026-11-02T11:01:00Z")}
    identity = {k: {"id": v["id"], "workflow_id": v["workflow_id"], "event": v["event"], "head_sha": v["head_sha"],
                    "created": mt.timestamp(v["created_at"]), "updated": mt.timestamp(v["updated_at"]),
                    "status": "completed", "conclusion": v["conclusion"]} for k, v in identity.items()}
    kept, _ = mt.partition_at_merge(runs, MERGE, runs=True, distinct_suites=True, run_of=lambda c: identity[c["id"]])
    assert [c["id"] for c in kept] == [2]
    identity[2][field] = value
    kept, _ = mt.partition_at_merge(runs, MERGE, runs=True, distinct_suites=True, run_of=lambda c: identity[c["id"]])
    assert [c["id"] for c in kept] == [1, 2]


def test_later_job_and_run_concluding_exactly_at_merge_supersede(evidence, actions):
    # "at or before" the merge: a conclusion in the merge's own second still gated it.
    evidence["runs"][1]["completed_at"] = MERGE
    actions["workflow_runs"][LATE]["updated_at"] = MERGE
    assert ids(collect()) == [2]


@pytest.mark.parametrize("tamper", [{"workflow_id": "5"}, {"workflow_id": True}, {"event": ""}, {"event": None}])
def test_malformed_identity_is_unprovable_even_when_both_runs_match(evidence, actions, tamper):
    for workflow_id in (EARLY, LATE):
        actions["workflow_runs"][workflow_id].update(tamper)
    assert 1 in ids(collect())


def test_without_a_workflow_run_resolver_cancelled_stays_evidence():
    from hermes_cli import kanban_pr_merge_time as mt

    runs = [check("cancelled", 1, 7, started="2026-11-02T11:00:30Z", completed="2026-11-02T11:02:00Z"),
            check("success", 2, 8, started="2026-11-02T11:01:30Z", completed="2026-11-02T12:30:00Z")]
    kept, _ = mt.partition_at_merge(runs, MERGE, runs=True, distinct_suites=True)
    assert [c["id"] for c in kept] == [1, 2]
