"""Merge-time evidence, using reduced recorded GitHub shapes from PR #656."""
from copy import deepcopy

import pytest

from hermes_cli import kanban_pr_acceptance as acceptance
from tests.hermes_cli.check_suite_fake import check_api

SHA = "850d52075dc59a3e842e123c9d986f864f370762"
MERGED = "2026-11-02T13:10:04Z"
NAME = "All required checks pass (v2)"
PRE = {"id": 125286307833, "name": NAME, "head_sha": SHA, "app": {"id": 15368},
       "status": "completed", "conclusion": "success", "started_at": "2026-11-02T12:51:52Z",
       "completed_at": "2026-11-02T12:52:08Z", "check_suite": {"id": 112312466991}}
POST = {"id": 125304345962, "name": NAME, "head_sha": SHA, "app": {"id": 15368},
        "status": "completed", "conclusion": "failure", "started_at": "2026-11-02T13:30:23Z",
        "completed_at": "2026-11-02T13:30:38Z", "check_suite": {"id": 112327246670}}


@pytest.fixture
def evidence(monkeypatch):
    state = {"runs": [deepcopy(POST), deepcopy(PRE)], "statuses": [], "state": "MERGED",
             "merged_at": MERGED, "head": SHA, "landed": SHA, "requests": [], "app": 15368}

    def api(endpoint, **kwargs):
        state["requests"].append(endpoint)
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": SHA, "baseRefName": "main", "state": state["state"],
                "mergedAt": state["merged_at"], "mergeCommit": {"oid": state["landed"]},
                "baseRef": {"branchProtectionRule": {
                    "requiredStatusChecks": [{"context": NAME, "app": {"databaseId": state["app"]}}]}}}}}}
        if "/rules/branches/" in endpoint:
            return [[]]
        if "/commits/" in endpoint and "/check-runs?" in endpoint and "filter=all" in endpoint:
            runs = state["runs"]
            return [{"total_count": len(runs) + state.get("omitted", 0), "check_runs": runs}]
        checks = check_api(endpoint, state["runs"], SHA)
        if checks is not None:
            return checks
        if "/statuses?" in endpoint:
            return [state["statuses"]]
        if endpoint.startswith("apps/"):
            apps = {"github-actions": 15368, "other-ci": 99}
            if endpoint[5:] not in apps:
                raise acceptance._GateAuthError(f"HTTP 404 on {endpoint}")
            return {"id": apps[endpoint[5:]], "slug": endpoint[5:]}
        if "/pulls/" in endpoint:
            # rest_state overrides model a PR whose state moved between the GraphQL and REST reads.
            rest = state.get("rest_state", state["state"])
            return {"head": {"sha": state["head"]}, "base": {"ref": "main"},
                    "state": "open" if rest == "OPEN" else "closed",
                    "merged": rest == "MERGED", "merged_at": state["merged_at"],
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


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_run_finishing_after_merge_remains_pending_at_merge(evidence, conclusion):
    evidence["runs"] = [{**PRE, "completed_at": POST["completed_at"], "conclusion": conclusion}]
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "pending"


def test_open_uses_latest_view_without_cutoff(evidence):
    # PR #656's pre- and post-merge runs live in different suites: both are latest.
    evidence["state"] = "OPEN"
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "failure"
    assert {c["id"]: c["classification"] for c in receipt["checks"]} == {
        PRE["id"]: "success", POST["id"]: "failure"}
    assert receipt["post_merge_checks"] == []
    assert [u for u in evidence["requests"] if "/check-" in u] == [
        f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=latest",
        f"repos/acme/repo/commits/{SHA}/check-suites?per_page=100"]


@pytest.mark.parametrize("result", ["failure", "missing", "pending"])
def test_later_success_never_repairs_premerge_evidence(evidence, result):
    pre = {**PRE, "conclusion": result, "status": "in_progress" if result == "pending" else "completed"}
    evidence["runs"] = [{**POST, "conclusion": "success"}] + ([] if result == "missing" else [pre])
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == result
    assert receipt["post_merge_checks"][0]["classification"] == "success"


def test_failed_premerge_rerun_overrides_earlier_success(evidence):
    evidence["runs"] = [PRE, {**POST, "started_at": MERGED, "completed_at": MERGED}]
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "failure"
    assert receipt["checks"][0]["id"] == POST["id"]
    assert not receipt["post_merge_checks"]


@pytest.mark.parametrize("conclusion", ["success", "failure", None])
def test_same_second_unfinished_run_after_concluded_premerge_is_post_merge(evidence, conclusion):
    # mergedAt has 1 s resolution: a post-merge push run can start in the merge's second.
    # It was not concluded at the merge, and an earlier same-key run was: that earlier
    # conclusion gated the merge, so the tie run is later main health, not pending evidence.
    tie = {**POST, "started_at": MERGED, "completed_at": "2026-11-02T13:10:20Z",
           "conclusion": conclusion}
    if conclusion is None:
        tie.update(status="in_progress", completed_at=None)
    evidence["runs"] = [PRE, tie]
    receipt = collect()
    assert receipt["ok"] and receipt["classification"] == "success", receipt
    assert [c["id"] for c in receipt["checks"]] == [PRE["id"]]
    assert [c["id"] for c in receipt["post_merge_checks"]] == [POST["id"]]
    assert receipt["post_merge_checks"][0]["conclusion"] == conclusion


@pytest.mark.parametrize("earlier", ["none", "unfinished", "same_second"])
def test_same_second_unfinished_run_stays_pending_without_earlier_conclusion(evidence, earlier):
    # No earlier conclusion at the merge to have gated it: ambiguous, fail closed.
    tie = {**POST, "started_at": MERGED, "completed_at": "2026-11-02T13:10:20Z",
           "conclusion": "success"}
    runs = [tie]
    if earlier == "unfinished":
        runs.append({**PRE, "completed_at": "2026-11-02T13:10:30Z"})
    elif earlier == "same_second":
        runs.append({**PRE, "started_at": MERGED, "completed_at": MERGED})
    evidence["runs"] = runs
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "pending", receipt
    assert not receipt["post_merge_checks"]


def test_successful_premerge_rerun_overrides_earlier_failure(evidence):
    evidence["runs"] = [{**PRE, "conclusion": "failure"},
                        {**POST, "started_at": MERGED, "completed_at": MERGED, "conclusion": "success"}]
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


@pytest.mark.parametrize("landed", ["rebased", "missing"])
def test_post_merge_red_on_unlanded_head_is_not_main_health(evidence, landed):
    # A rebase landed another SHA: a later red run on the PR head is not base CI.
    evidence["landed"] = "b" * 40 if landed == "rebased" else None
    receipt = collect()
    assert receipt["ok"] and receipt["classification"] == "success"
    assert [c["id"] for c in receipt["post_merge_checks"]] == [POST["id"]]
    assert receipt["post_merge_checks"][0]["classification"] == "failure"
    assert "main_health" not in receipt


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
                    status({**POST, "started_at": MERGED, "completed_at": MERGED}, "failure")])
    assert collect()["classification"] == "failure"


def test_pinned_app_cannot_borrow_legacy_success(evidence):
    evidence.update(runs=[], statuses=[status(PRE, "success")])
    assert collect()["classification"] == "missing"


ACTIONS_BOT = {"id": 41898282, "login": "github-actions[bot]", "type": "Bot"}


def test_pinned_app_accepts_its_own_legacy_status(evidence):
    # Metta-AI/fabric-research#199: "Human review approval" is pinned to integration 15368
    # and satisfied by a commit status github-actions[bot] posted; GitHub merged it CLEAN.
    evidence.update(runs=[], statuses=[{**status(PRE, "success"), "creator": ACTIONS_BOT}])
    receipt = collect()
    assert receipt["ok"] and receipt["classification"] == "success", receipt
    assert [c["id"] for c in receipt["checks"]] == [PRE["id"]]
    assert evidence["requests"].count("apps/github-actions") == 1


@pytest.mark.parametrize("creator", [
    {"id": 7, "login": "other-ci[bot]", "type": "Bot"},       # a different app
    {"id": 8, "login": "ghost-bot[bot]", "type": "Bot"},      # no app behind the bot (404)
    {"id": 9, "login": "github-actions[bot]", "type": "User"},  # not a bot user
    {"id": 10, "login": "someone", "type": "User"}])
def test_pinned_app_rejects_legacy_status_from_anyone_else(evidence, creator):
    evidence.update(runs=[], statuses=[{**status(PRE, "success"), "creator": creator}])
    assert collect()["classification"] == "missing"


def test_pinned_app_status_not_hidden_by_later_status_from_another_creator(evidence):
    # The latest pre-merge status per context belongs to someone else: the app's own
    # failure is still the evidence, never borrowed success nor "missing".
    late = {**PRE, "id": PRE["id"] + 1, "started_at": "2026-11-02T13:00:00Z"}
    evidence.update(runs=[], statuses=[
        {**status(PRE, "failure"), "creator": ACTIONS_BOT},
        {**status(late, "success"), "creator": {"id": 10, "login": "someone", "type": "User"}}])
    assert collect()["classification"] == "failure"


def test_unpinned_context_retains_each_app_failure(evidence):
    evidence.update(app=None, runs=[PRE, {**PRE, "id": PRE["id"] + 1,
                                         "app": {"id": 99}, "conclusion": "failure"}])
    assert collect()["classification"] == "failure"


def test_unpinned_context_retains_older_other_app_failure(evidence):
    # Mirror of the case above: the other app's failure is OLDER than the same-named
    # success, so a name-only key would let the newer success replace it.
    older = {**PRE, "id": PRE["id"] - 1, "app": {"id": 99}, "conclusion": "failure",
             "started_at": "2026-11-02T12:40:00Z", "completed_at": "2026-11-02T12:41:00Z"}
    evidence.update(app=None, runs=[PRE, older])
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "failure"
    assert {c["id"] for c in receipt["checks"]} == {PRE["id"], older["id"]}


def test_pr_merged_during_collection_is_stale(evidence):
    # GraphQL read the PR as OPEN; the later REST re-read sees it merged.
    evidence.update(state="OPEN", rest_state="MERGED", runs=[PRE])
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "stale"
    assert "merge state changed" in receipt["detail"]


def test_all_naive_merge_chronology_fails_closed(evidence):
    # Naive timestamps compare fine with each other, so only the explicit tz check rejects them.
    def naive(value):
        return value.removesuffix("Z")

    evidence["merged_at"] = naive(MERGED)
    evidence["runs"] = [{**run, "started_at": naive(run["started_at"]),
                         "completed_at": naive(run["completed_at"])} for run in (POST, PRE)]
    receipt = collect()
    assert not receipt["ok"] and receipt["classification"] == "infra"


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


def test_completed_landing_proof_survives_github_expiry(evidence, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    import json

    url = "https://github.com/acme/repo/actions/runs/123/job/456"
    evidence["runs"][1]["html_url"] = url
    kb.init_db()
    with connect() as conn:
        task = kb.create_task(conn, title="Archive landing", completion_contract="acme/repo")
        assert kb.complete_task(conn, task, summary="verified", metadata={
            "published_pr": "https://github.com/acme/repo/pull/7"})
    # The producer has completed. No GitHub result remains for the auditor.
    evidence["runs"] = []
    monkeypatch.setattr(acceptance, "_api", lambda *a, **kw: pytest.fail("offline audit contacted GitHub"))
    with connect() as conn:
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (task,)).fetchone()[0])
    assert receipt["ok"] and receipt["pr_state"] == "MERGED"
    row, = receipt["checks"]
    assert {key: row[key] for key in ("name", "head_sha", "conclusion", "completed_at", "html_url")} == {
        "name": NAME, "head_sha": SHA, "conclusion": "success",
        "completed_at": PRE["completed_at"], "html_url": url}


def test_commit_status_landing_proof_survives_github_expiry(evidence, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    import json

    required_status = status(PRE, "success")
    evidence.update(app=None, runs=[], statuses=[required_status])
    kb.init_db()
    with connect() as conn:
        task = kb.create_task(conn, title="Archive status landing", completion_contract="acme/repo")
        assert kb.complete_task(conn, task, summary="verified", metadata={
            "published_pr": "https://github.com/acme/repo/pull/7"})
    # Reopen the stored proof after GitHub's status evidence is unavailable.
    evidence["statuses"] = []
    monkeypatch.setattr(acceptance, "_api", lambda *a, **kw: pytest.fail("offline audit contacted GitHub"))
    with connect() as conn:
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (task,)).fetchone()[0])
    assert receipt["ok"] and receipt["pr_state"] == "MERGED"
    row, = receipt["checks"]
    assert row["id"] == required_status["id"]
    assert row["name"] == NAME and row["conclusion"] == "success"
    assert row["completed_at"] == required_status["created_at"]
    assert row["html_url"] == required_status["target_url"]


@pytest.mark.parametrize("owner_state", ["ready", "done", "archived", None])
def test_completion_receipt_names_main_health_owner(evidence, owner_state):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    import json

    kb.init_db()
    with connect() as conn:
        owner = None
        if owner_state:
            owner = kb.create_task(conn, title="Main health", created_by="ci-main-watch",
                                   idempotency_key=f"ci-red:acme/repo:{SHA}")
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (owner_state, owner))
        task = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
        assert kb.complete_task(conn, task, summary="verified", metadata={
            "published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (task,)).fetchone()[0])
        assert receipt["ok"]
        active_owner = owner if owner_state == "ready" else None
        assert receipt["main_health"].get("owner_card") == active_owner
        assert "ci-main-watch" in receipt["main_health"]["detail"]
        if active_owner:
            assert active_owner in receipt["main_health"]["detail"]
