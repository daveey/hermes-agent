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
                "mergeCommit": {"oid": state["landed"]} if state.get("landed") else None,
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
            rows = list(state.get("extra_suites", []))
            if state["runs"] or state.get("empty_suite"):
                rows.insert(0, {"id": 7, "app": {"id": 1}, "head_sha": SHA,
                                "latest_check_runs_count": len(state["runs"])})
            return [{"total_count": len(rows), "check_suites": rows}]
        if "/actions/jobs/" in endpoint:
            job = state.get("jobs", {}).get(int(endpoint.rsplit("/", 1)[1]))
            if job is None:
                raise gate._GateAuthError("HTTP 404 on " + endpoint)
            return job
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


@pytest.mark.parametrize("outcome,expected", [("failure", "failure"), ("pending", "pending"),
                                             ("cancelled", "infra"), ("success", "success")])
def test_merged_fallback_preserves_concurrent_same_suite_siblings(evidence, outcome, expected):
    # Same start (a tie with the first run's end) can be a sibling job, not a rerun.
    evidence.update(merged=True, runs=[run(outcome), run("success", id=2)])
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == expected
    assert receipt["ok"] is (expected == "success")
    assert [c["id"] for c in receipt["checks"]] == [1, 2]


def rerun_pair(first, *, second="success", second_start="2026-11-02T12:30:00Z", suite=7,
               first_end="2026-11-02T12:10:00Z"):
    earlier = {**run(first), "completed_at": first_end}
    later = {**run(second, id=2, started=second_start), "check_suite": {"id": suite}}
    return [earlier, later]


ACTIONS = 15368


def as_actions(evidence, runs, attempts, *, run_id=99):
    """Make ``runs`` Actions jobs of workflow run ``run_id`` at the given attempts."""
    for check in runs:
        check["app"] = {"id": ACTIONS}
    evidence["jobs"] = {c["id"]: {"run_id": run_id, "run_attempt": a, "head_sha": SHA,
                                  "check_run_url": f"https://api.github.com/repos/acme/repo/check-runs/{c['id']}"}
                        for c, a in zip(runs, attempts)}
    return runs


@pytest.mark.parametrize("first", ["failure", "cancelled", "timed_out"])
def test_merged_fallback_rerun_before_merge_supersedes_failure(evidence, first):
    # hermes-mods #183/#178: a failed job re-run to success before the merge.
    evidence.update(merged=True, runs=as_actions(evidence, rerun_pair(first), [1, 2]))
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == "success"
    assert receipt["ok"] is True
    assert [c["id"] for c in receipt["checks"]] == [2]


@pytest.mark.parametrize("second,expected", [("failure", "failure"), ("pending", "pending"),
                                             ("cancelled", "infra")])
def test_merged_fallback_red_rerun_stays_red(evidence, second, expected):
    evidence.update(merged=True, runs=rerun_pair("success", second=second))
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == expected
    assert receipt["ok"] is False


@pytest.mark.parametrize("kw", [
    {"second_start": "2026-11-02T12:05:00Z"},  # overlaps the failed run
    {"second_start": "2026-11-02T12:10:00Z"},  # starts the second the failure ended
    {"suite": 8},                              # another workflow suite, not a rerun
])
def test_merged_fallback_unproven_supersession_keeps_failure(evidence, kw):
    evidence.update(merged=True, runs=as_actions(evidence, rerun_pair("failure", **kw), [1, 2]))
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == "failure"
    assert receipt["ok"] is False
    assert [c["id"] for c in receipt["checks"]] == [1, 2]


def test_merged_fallback_pending_run_is_never_superseded(evidence):
    evidence.update(merged=True, runs=rerun_pair("pending"))
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == "pending"
    assert [c["id"] for c in receipt["checks"]] == [1, 2]


def test_merged_fallback_rerun_after_merge_keeps_failure(evidence):
    # Green-after-merge is post-merge evidence; the merge was gated on red.
    evidence.update(merged=True, runs=rerun_pair("failure", second_start="2026-11-02T13:30:00Z"))
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == "failure"
    assert [c["classification"] for c in receipt["post_merge_checks"]] == ["success"]


def test_merged_fallback_rerun_finishing_after_merge_is_pending(evidence):
    runs = rerun_pair("failure", second_start="2026-11-02T12:50:00Z")
    runs[1]["completed_at"] = "2026-11-02T13:10:00Z"
    evidence.update(merged=True, runs=as_actions(evidence, runs, [1, 2]))
    assert gate.collect_acceptance(URL, None)["classification"] == "pending"


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


OLD = "2026-01-01T00:00:00Z"


def suite(id, app, *, status="queued", created=OLD, count=0):
    return {"id": id, "app": {"id": app, "slug": f"app-{app}"}, "head_sha": SHA,
            "status": status, "created_at": created, "latest_check_runs_count": count}


@pytest.mark.parametrize("merged", [False, True])
def test_idle_third_party_suites_do_not_strand_green_ci(evidence, merged):
    # hermes-mods PRs carry 0-run queued suites from installed apps that never run.
    evidence.update(merged=merged, runs=[run("success")],
                    extra_suites=[suite(100 + n, 900 + n) for n in range(5)])
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == "success"
    assert receipt["ok"] is True
    assert [s["id"] for s in receipt["idle_suites"]] == [100, 101, 102, 103, 104]


@pytest.mark.parametrize("outcome,expected", [("failure", "failure"), ("pending", "pending"),
                                             ("cancelled", "infra")])
def test_idle_suites_never_hide_red_ci(evidence, outcome, expected):
    evidence.update(runs=[run(outcome)], extra_suites=[suite(100, 900)])
    assert gate.collect_acceptance(URL, None)["classification"] == expected


@pytest.mark.parametrize("merged", [False, True])
def test_only_idle_suites_is_waivable_absence(evidence, merged):
    evidence.update(merged=merged, extra_suites=[suite(100, 900), suite(101, 901)])
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == "no_required_checks"
    assert receipt["ok"] is False


@pytest.mark.parametrize("live", [
    suite(100, 1),                                    # same app as a producing suite
    suite(100, 900, status="in_progress"),            # started
    suite(100, 900, created="2999-01-01T00:00:00Z"),  # inside the grace period
    {**suite(100, 900), "created_at": None},          # unreadable age
    {**suite(100, 900), "app": None},                 # unreadable app
    suite(100, 15368),                                # github-actions: may be concurrency-held
])
def test_possibly_live_empty_suite_stays_missing(evidence, live):
    evidence.update(runs=[run("success")], extra_suites=[live])
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] in ("missing", "infra")
    assert receipt["ok"] is False
    assert not receipt.get("idle_suites")


@pytest.mark.parametrize("same_app", [True, False])
def test_nonempty_sibling_suite_marks_app_active_without_runs(same_app):
    from datetime import UTC, datetime
    from hermes_cli.kanban_pr_fallback import idle_suites

    # Exercise the inventory policy directly: collector completeness checks can
    # reject missing runs before reaching it and thus mask this regression.
    empty = suite(100, 900)
    active = suite(101, 900 if same_app else 901, count=1)
    assert idle_suites([empty, active], [], datetime(2026, 11, 2, tzinfo=UTC)) == (
        [] if same_app else [empty])


@pytest.mark.parametrize("started,expected", [
    ("2026-11-02T13:00:00Z", "pending"),
    ("2026-11-02T13:00:01Z", "success"),
])
def test_merge_cutoff_sibling_does_not_inherit_earlier_run_gate(evidence, started, expected):
    earlier = run("success")
    sibling = {**run("success", id=2, started=started),
               "completed_at": "2026-11-02T13:10:00Z"}
    evidence.update(merged=True, runs=[earlier, sibling])
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == expected
    assert receipt["ok"] is (expected == "success")
    assert [(c["id"], c["classification"]) for c in receipt["checks"]] == (
        [(1, "success"), (2, "pending")] if expected == "pending" else [(1, "success")])
    assert [(c["id"], c["classification"]) for c in receipt["post_merge_checks"]] == (
        [] if expected == "pending" else [(2, "success")])


def test_possibly_live_empty_suite_blocks_absence_waiver(evidence):
    evidence.update(merged=True, extra_suites=[suite(100, 900, status="in_progress")])
    assert gate.collect_acceptance(URL, None)["classification"] == "missing"


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
@pytest.mark.parametrize("merged", [False, True])
def test_fallback_accepts_skipped_and_neutral(evidence, conclusion, merged):
    evidence.update(merged=merged, runs=[run("success"), {**run(conclusion, id=2), "name": "cond"}])
    receipt = gate.collect_acceptance(URL, None)
    assert receipt["classification"] == "success"
    assert [c["conclusion"] for c in receipt["checks"]] == ["success", conclusion]


def test_skipped_on_another_head_is_not_success(evidence):
    evidence["runs"] = [{**run("skipped"), "head_sha": "b" * 40}]
    assert gate.collect_acceptance(URL, None)["classification"] != "success"


def test_required_checks_keep_strict_conclusions():
    from hermes_cli.kanban_pr_fallback import fallback_outcomes

    receipt = {"head_sha": SHA, "checks": [{"classification": "infra"}]}
    assert fallback_outcomes(receipt, {("ci", None)}, ["infra"], [run("skipped")], [], [],
                             merged=False) == ["infra"]
    assert receipt["checks"] == [{"classification": "infra"}]
