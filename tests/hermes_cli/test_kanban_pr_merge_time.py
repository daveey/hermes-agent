"""Merge-time evidence, using reduced recorded GitHub shapes from PR #656."""
from copy import deepcopy

import pytest

from hermes_cli import kanban_pr_acceptance as acceptance

SHA = "850d52075dc59a3e842e123c9d986f864f370762"
MERGED = "2026-11-02T13:10:04Z"
NAME = "All required checks pass (v2)"
PRE = {"id": 125286307833, "name": NAME, "head_sha": SHA, "app": {"id": 15368},
       "status": "completed", "conclusion": "success", "started_at": "2026-11-02T12:51:52Z",
       "completed_at": "2026-11-02T12:52:08Z"}
POST = {"id": 125304345962, "name": NAME, "head_sha": SHA, "app": {"id": 15368},
        "status": "completed", "conclusion": "failure", "started_at": "2026-11-02T13:30:23Z",
        "completed_at": "2026-11-02T13:30:38Z"}


@pytest.fixture
def evidence(monkeypatch):
    state = {"runs": [deepcopy(POST), deepcopy(PRE)], "statuses": [], "state": "MERGED",
             "merged_at": MERGED, "head": SHA, "landed": SHA, "requests": [], "app": 15368}

    def api(endpoint, **kwargs):
        state["requests"].append(endpoint)
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": SHA, "baseRefName": "main", "state": state["state"],
                "mergedAt": state["merged_at"], "baseRef": {"branchProtectionRule": {
                    "requiredStatusChecks": [{"context": NAME, "app": {"databaseId": state["app"]}}]}}}}}}
        if "/rules/branches/" in endpoint:
            return [[]]
        if "/check-runs?" in endpoint:
            runs = state["runs"]
            if "filter=latest" in endpoint:
                # GitHub hides earlier executions for the same check name.
                runs = [max(runs, key=lambda r: r["id"])] if runs else []
            return [{"total_count": len(runs) + state.get("omitted", 0), "check_runs": runs}]
        if "/statuses?" in endpoint:
            return [state["statuses"]]
        if "/pulls/" in endpoint:
            return {"head": {"sha": state["head"]}, "base": {"ref": "main"},
                    "state": "open" if state["state"] == "OPEN" else "closed",
                    "merged": state["state"] == "MERGED", "merged_at": state["merged_at"],
                    "merge_commit_sha": state["landed"]}
        pytest.fail(f"Unexpected API call: {endpoint}")

    monkeypatch.setattr(acceptance, "_api", api)
    return state


def collect():
    return acceptance.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")


def test_merged_fast_forward_uses_premerge_success(evidence):
    receipt = collect()
    assert receipt["ok"], receipt
    assert [c["id"] for c in receipt["checks"]] == [PRE["id"]]
    assert receipt["post_merge_checks"][0]["classification"] == "failure"
    assert "ci-main-watch" in receipt["main_health"]["detail"]
    assert "rerun" not in receipt["recovery"].lower()


def test_open_still_uses_latest_without_cutoff(evidence):
    evidence["state"] = "OPEN"
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "failure"
    assert receipt["checks"][0]["id"] == POST["id"]
    assert receipt["post_merge_checks"] == []
    assert any("filter=latest" in url for url in evidence["requests"])


@pytest.mark.parametrize("result", ["failure", "missing", "pending"])
def test_later_success_never_repairs_premerge_evidence(evidence, result):
    pre = {**PRE, "conclusion": result, "status": "in_progress" if result == "pending" else "completed"}
    evidence["runs"] = [{**POST, "conclusion": "success"}] + ([] if result == "missing" else [pre])
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == result
    assert receipt["post_merge_checks"][0]["classification"] == "success"


def test_failed_premerge_rerun_overrides_earlier_success(evidence):
    evidence["runs"] = [PRE, {**POST, "started_at": MERGED}]
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "failure"
    assert receipt["checks"][0]["id"] == POST["id"]
    assert not receipt["post_merge_checks"]


def test_successful_premerge_rerun_overrides_earlier_failure(evidence):
    evidence["runs"] = [{**PRE, "conclusion": "failure"},
                        {**POST, "started_at": MERGED, "conclusion": "success"}]
    assert collect()["ok"]


@pytest.mark.parametrize("fault", ["head_change", "stale_run", "pagination"])
def test_merge_selection_preserves_evidence_guards(evidence, fault):
    if fault == "head_change":
        evidence["head"] = "b" * 40
    elif fault == "stale_run":
        evidence["runs"][1]["head_sha"] = "b" * 40
    else:
        evidence["omitted"] = 1
    receipt = collect()
    assert not receipt["ok"]
    assert receipt["classification"] == ("infra" if fault == "pagination" else "stale")


def test_rebased_landing_still_uses_original_head(evidence):
    evidence["landed"] = "b" * 40
    evidence["runs"] = [PRE]
    receipt = collect()
    assert receipt["ok"] and receipt["head_sha"] == SHA
    assert not receipt["post_merge_checks"]
    assert not any(evidence["landed"] in url for url in evidence["requests"])


def status(check, result):
    return {"id": check["id"], "context": NAME, "state": result,
            "created_at": check["started_at"], "target_url": "https://example.com/ci"}


@pytest.mark.parametrize("pre,post,expected", [("success", "failure", "success"),
                                               ("failure", "success", "failure"),
                                               ("pending", "success", "pending"),
                                               (None, "success", "missing")])
def test_legacy_statuses_obey_merge_cutoff(evidence, pre, post, expected):
    evidence.update(app=None, runs=[], statuses=[status(POST, post)])
    if pre:
        evidence["statuses"].append(status(PRE, pre))
    receipt = collect()
    assert receipt["classification"] == expected
    assert receipt["ok"] is (expected == "success")
    assert receipt["post_merge_checks"][0]["classification"] == post


def test_legacy_latest_premerge_failure_wins(evidence):
    evidence.update(app=None, runs=[], statuses=[status(PRE, "success"),
                    status({**POST, "started_at": MERGED}, "failure")])
    assert collect()["classification"] == "failure"


def test_pinned_app_cannot_borrow_legacy_success(evidence):
    evidence.update(runs=[], statuses=[status(PRE, "success")])
    assert collect()["classification"] == "missing"


def test_unpinned_context_retains_each_app_failure(evidence):
    evidence.update(app=None, runs=[PRE, {**PRE, "id": PRE["id"] + 1,
                                         "app": {"id": 99}, "conclusion": "failure"}])
    assert collect()["classification"] == "failure"


@pytest.mark.parametrize("field,value", [("merged_at", None), ("merged_at", "invalid"),
                                        ("started_at", None), ("started_at", "2026-11-02T12:00:00")])
def test_unknown_merge_chronology_fails_closed(evidence, field, value):
    if field == "merged_at":
        evidence[field] = value
    else:
        evidence["runs"][1][field] = value
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "infra"


@pytest.mark.parametrize("conclusion", ["skipped", "neutral", "cancelled", "success"])
def test_nonfailure_postmerge_checks_do_not_claim_main_is_red(evidence, conclusion):
    evidence["runs"][0]["conclusion"] = conclusion
    receipt = collect()
    assert receipt["ok"] and "main_health" not in receipt
    assert receipt["post_merge_checks"][0]["conclusion"] == conclusion


@pytest.mark.parametrize("owner_state", ["ready", "done", "archived", None])
def test_completion_receipt_names_main_health_owner(evidence, owner_state):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    import json

    kb.init_db()
    with connect() as conn:
        owner = None
        if owner_state:
            owner = kb.create_task(conn, title="Main health", idempotency_key=f"ci-red:acme/repo:{SHA}")
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (owner_state, owner))
        task = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
        assert kb.complete_task(conn, task, summary="verified", metadata={
            "published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (task,)).fetchone()[0])
        assert receipt["ok"]
        assert receipt["main_health"].get("owner_card") == owner
        assert "ci-main-watch" in receipt["main_health"]["detail"]
        if owner:
            assert owner in receipt["main_health"]["detail"]
