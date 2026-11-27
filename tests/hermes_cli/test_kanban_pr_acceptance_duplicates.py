"""Required duplicate names need a real success, never merely a soft conclusion."""
import pytest

from hermes_cli import kanban_pr_acceptance as acceptance

SHA = "5cae568e287c30176df46a3fa2eaefd26b50b679"
URL = "https://github.com/softmax-universe/coglet/pull/211"


def run(identifier, conclusion, *, app=15368, sha=SHA, status="completed"):
    return {"id": identifier, "name": "conformance", "head_sha": sha,
            "status": status, "conclusion": conclusion, "app": {"id": app},
            "started_at": "2026-11-27T13:25:13Z", "completed_at": "2026-11-27T13:25:13Z",
            "html_url": f"https://github.com/softmax-universe/coglet/actions/runs/{identifier}",
            "check_suite": {"id": identifier}}


def collect(monkeypatch, runs, state="MERGED", app: int | None = 15368):
    def api(endpoint, **_kwargs):
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": SHA, "baseRefName": "main", "state": state,
                "mergedAt": "2026-11-27T15:40:26Z", "mergeCommit": {"oid": "2" * 40},
                "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                    {"context": "conformance", "app": {"databaseId": app}}]}}}}}}
        if "/rules/branches/" in endpoint or "/statuses?" in endpoint:
            return [[]]
        if "/check-runs?" in endpoint:
            return [{"total_count": len(runs), "check_runs": runs}]
        if "/check-suites?" in endpoint:
            return [{"total_count": len(runs), "check_suites": [
                {"id": r["check_suite"]["id"], "head_sha": SHA,
                 "app": r["app"], "latest_check_runs_count": 1} for r in runs]}]
        if endpoint.endswith("/pulls/211"):
            return {"head": {"sha": SHA}, "base": {"ref": "main"},
                    "state": "closed" if state == "MERGED" else "open", "merged": state == "MERGED"}
        pytest.fail(f"Unexpected GitHub read: {endpoint}")
    monkeypatch.setattr(acceptance, "_api", api)
    return acceptance.collect_acceptance(URL, URL)


@pytest.mark.parametrize("state", ["OPEN", "MERGED"])
@pytest.mark.parametrize("conclusions,expected", [
    (["success", "skipped"], "success"),
    (["skipped"], "infra"),
    (["success", "failure"], "failure"),
    (["success", "neutral"], "success"),
    (["neutral"], "infra"),
    (["skipped", "neutral"], "infra"),
    ([], "missing"),
    (["success", "cancelled"], "infra"),
    (["success", "timed_out"], "infra"),
    (["success", "action_required"], "infra"),
])
def test_required_duplicate_conclusions(monkeypatch, state, conclusions, expected):
    receipt = collect(monkeypatch, [run(i + 1, c) for i, c in enumerate(conclusions)], state)
    assert receipt["classification"] == expected, receipt
    assert receipt["ok"] is (expected == "success")


def test_coglet_211_pair(monkeypatch):
    # Reduced live API fixture, read 2026-11-27. Different workflows/suites,
    # same Actions app and exact PR head; both completed before merge.
    success = {**run(140514951868, "success"),
               "started_at": "2026-11-27T12:48:48Z", "completed_at": "2026-11-27T13:14:02Z",
               "check_suite": {"id": 124451024459},
               "html_url": "https://github.com/softmax-universe/coglet/actions/runs/46001772711/job/140514951868"}
    skipped = {**run(140532373733, "skipped"), "check_suite": {"id": 124465218523},
               "html_url": "https://github.com/softmax-universe/coglet/actions/runs/46006943418/job/140532373733"}
    receipt = collect(monkeypatch, [skipped, success])
    assert receipt["classification"] == "success", receipt
    assert {c["id"] for c in receipt["checks"]} == {success["id"], skipped["id"]}


@pytest.mark.parametrize("fault", ["foreign_app", "stale", "pending", "post_merge", "missing_completion"])
def test_soft_conclusion_cannot_borrow_invalid_success(monkeypatch, fault):
    success = run(1, "success")
    if fault == "foreign_app":
        success["app"] = {"id": 9}
    elif fault == "stale":
        success["head_sha"] = "b" * 40
    elif fault == "pending":
        success["status"] = "in_progress"
    elif fault == "missing_completion":
        success["completed_at"] = None
    else:
        success.update(started_at="2026-11-27T16:00:00Z", completed_at="2026-11-27T16:01:00Z")
    receipt = collect(monkeypatch, [success, run(2, "skipped")], app=None)
    assert not receipt["ok"], receipt


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required", None])
def test_merged_soft_run_never_resurrects_superseded_success(monkeypatch, conclusion):
    receipt = collect(monkeypatch, [run(1, "success"), run(2, conclusion), run(3, "skipped")])
    assert not receipt["ok"], receipt


@pytest.mark.parametrize("state", ["OPEN", "MERGED"])
@pytest.mark.parametrize("fault", ["stale", "pending"])
def test_invalid_soft_run_stays_non_success(monkeypatch, state, fault):
    soft = run(2, "skipped", sha="b" * 40 if fault == "stale" else SHA,
               status="in_progress" if fault == "pending" else "completed")
    assert not collect(monkeypatch, [run(1, "success"), soft], state)["ok"]


@pytest.mark.parametrize("state", ["OPEN", "MERGED"])
@pytest.mark.parametrize("soft", ["skipped", "neutral"])
def test_success_latest_and_input_order_independent(monkeypatch, state, soft):
    rows = [run(1, soft), run(2, "success")]
    for ordered in (rows, rows[::-1]):
        assert collect(monkeypatch, ordered, state)["ok"]
