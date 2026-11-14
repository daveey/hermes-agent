"""No-required policy must inspect CI before offering an absence waiver."""
import pytest

from hermes_cli import kanban_pr_acceptance as gate

SHA = "a" * 40
URL = "https://github.com/acme/repo/pull/7"


@pytest.fixture
def evidence(monkeypatch):
    state = {"runs": [], "statuses": [], "merged": False}

    def api(endpoint, **kwargs):
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": SHA, "baseRefName": "main",
                "state": "MERGED" if state["merged"] else "OPEN",
                "mergedAt": "2026-11-02T13:00:00Z",
                "baseRef": {"branchProtectionRule": None}}}}}
        if "/rules/" in endpoint:
            return [[]]
        if state.get("unreadable"):
            raise ValueError("unreadable evidence")
        if "/check-runs?" in endpoint:
            return [{"total_count": len(state["runs"]) + state.get("missing", 0),
                     "check_runs": state["runs"]}]
        if "/check-suites?" in endpoint:
            if state.get("missing_suites"):
                return [{"total_count": 1, "check_suites": []}]
            if not state["runs"] and not state.get("empty_suite"):
                return [{"total_count": 0, "check_suites": []}]
            return [{"total_count": 1, "check_suites": [{"id": 7, "app": {"id": 1},
                     "head_sha": SHA, "latest_check_runs_count": len(state["runs"])}]}]
        if "/statuses?" in endpoint:
            return state.get("status_pages", [state["statuses"]])
        if "/pulls/" in endpoint:
            return {"head": {"sha": SHA}, "base": {"ref": "main"},
                    "state": "closed" if state["merged"] else "open", "merged": state["merged"]}
        raise AssertionError(endpoint)

    monkeypatch.setattr(gate, "_api", api)
    return state


def run(outcome, *, id=1, started="2026-11-02T12:00:00Z"):
    return {"id": id, "name": "ci", "head_sha": SHA, "app": {"id": 1},
            "check_suite": {"id": 7}, "started_at": started,
            "completed_at": started, "status": "in_progress" if outcome == "pending" else "completed",
            "conclusion": outcome}


@pytest.mark.parametrize("outcome,expected", [("failure", "failure"), ("pending", "pending"),
                                             ("cancelled", "infra"), ("success", "success")])
@pytest.mark.parametrize("merged", [False, True])
def test_fallback_run_outcomes(evidence, outcome, expected, merged):
    evidence.update(runs=[run(outcome)], merged=merged)
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == expected
    assert receipt["ok"] is (expected == "success")
    assert receipt["checks"][0]["id"] == 1


@pytest.mark.parametrize("fault", ["unreadable", "missing", "missing_suites"])
def test_fallback_incomplete_is_not_waivable(evidence, fault):
    evidence[fault] = 1
    assert gate.collect_acceptance(URL, None)["classification"] == "infra"


@pytest.mark.parametrize("merged", [False, True])
def test_fallback_empty_is_explicit_absence(evidence, merged):
    evidence["merged"] = merged
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == "no_required_checks"
    assert receipt["ok"] is False
    assert receipt["checks"] == []


@pytest.mark.parametrize("merged", [False, True])
def test_fallback_empty_suite_is_not_absence(evidence, merged):
    evidence.update(merged=merged, empty_suite=True)
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == "missing"
    assert receipt["ok"] is False


@pytest.mark.parametrize("pages", [[], {}, [None], [{}]])
def test_fallback_status_pages_must_be_present(evidence, pages):
    evidence["status_pages"] = pages
    assert gate.collect_acceptance(URL, None)["classification"] == "infra"


def test_empty_suite_cannot_hide_behind_successful_status(evidence):
    evidence.update(empty_suite=True, statuses=[{"id": 1, "context": "ci", "state": "success"}])
    assert gate.collect_acceptance(URL, None)["classification"] == "missing"


def test_post_merge_evidence_is_not_absence_after_suite_disappears():
    from hermes_cli.kanban_pr_fallback import mark_absence

    receipt = {"classification": "missing", "post_merge_checks": [{"classification": "success"}]}
    mark_absence(receipt, set(), [], [{"total_count": 0, "check_suites": []}], "acme/repo@main")
    assert receipt["classification"] == "missing"


def test_merged_fallback_keeps_same_name_workflows_distinct(evidence):
    failed = run("failure")
    passed = {**run("success", id=2), "check_suite": {"id": 8}}
    evidence.update(merged=True, runs=[failed, passed])
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == "failure"
    assert [c["id"] for c in receipt["checks"]] == [1, 2]


def test_fallback_merge_cutoff(evidence):
    evidence.update(merged=True, runs=[run("failure"), run("success", id=2, started="2026-11-02T14:00:00Z")])
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == "failure"
    assert receipt["post_merge_checks"][0]["classification"] == "success"
    evidence["runs"] = evidence["runs"][1:]
    assert gate.collect_acceptance(URL, None)["classification"] == "missing"


@pytest.mark.parametrize("outcome,expected", [("failure", "failure"), ("pending", "pending"),
                                             ("cancelled", "infra"), ("success", "success")])
def test_waiver_cannot_hide_fallback_ci(evidence, tmp_path, monkeypatch, outcome, expected):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    import json

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    evidence.update(merged=True, runs=[run(outcome)])
    with connect() as conn:
        tid = kb.create_task(conn, title="Publish", completion_contract=URL)
        ok = kb.complete_task(conn, tid, summary="verified", metadata={
            "contract_waiver": {"reason": "local tests passed"}})
        assert ok is (expected == "success")
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)
        ).fetchone()[0])
        assert receipt["classification"] == expected
        assert (kb.get_task(conn, tid).status == "done") is ok
        assert not conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='contract_waived'", (tid,)
        ).fetchone()


def test_fallback_statuses_keep_producers_distinct(evidence):
    evidence["statuses"] = [
        {"id": 1, "context": "ci", "state": "failure", "creator": {"id": 1}},
        {"id": 2, "context": "ci", "state": "success", "creator": {"id": 2}},
    ]
    assert gate.collect_acceptance(URL, None)["classification"] == "failure"
    evidence["statuses"].append({"id": 3, "context": "ci", "state": "success", "creator": {"id": 1}})
    assert gate.collect_acceptance(URL, None)["classification"] == "success"
