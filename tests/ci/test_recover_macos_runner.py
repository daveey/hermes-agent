"""Behavior tests for scripts/ci/recover_macos_runner.py (macOS runner-loss recovery).

Fixture provenance: the RUNNER_LOSS_* and TEST_FAILURE_* payloads below are trimmed
copies of REAL historical GitHub API responses, captured 2026-11-01 with
``gh api`` (token-broker scope user/github) from daveey/hermes:

* run 41003441463 attempt 1 (runner lost after a macOS update on the mini):
  ``actions/runs/41003441463``, ``.../attempts/1/jobs``,
  ``check-runs/123892862167/annotations``;
* run 40751299089 attempt 1 (an ordinary test failure):
  ``actions/runs/40751299089``, ``.../attempts/1/jobs``,
  ``check-runs/123038389129/annotations``.

They are historical evidence, not a live API read; every API call in these tests
goes to an in-memory fake. Fields the script does not read were dropped.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "recover_macos_runner.py"

_spec = importlib.util.spec_from_file_location("recover_macos_runner", SCRIPT)
assert _spec is not None and _spec.loader is not None
recover = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = recover  # dataclasses resolve their module by name
_spec.loader.exec_module(recover)

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
RECOVERY_WORKFLOW = WORKFLOWS / "hermes-state-recovery.yml"
_YAML = YAML(typ="safe")

REPO = "softmax-universe/daveey-hermes"
_REPO_OBJ = {"full_name": REPO}

# --- run 41003441463: runner lost communication (real, historical) -----------------
RUNNER_LOSS_RUN = {
    "id": 41003441463, "name": "hermes_state (macOS)",
    "path": ".github/workflows/hermes-state-macos.yml", "event": "push",
    "status": "completed", "conclusion": "failure", "run_attempt": 1,
    "head_branch": "main", "head_sha": "d72202494966a92e772ebe5546beba613de2504c",
    "workflow_id": 399465002, "repository": _REPO_OBJ, "head_repository": _REPO_OBJ,
}
RUNNER_LOSS_JOBS = {"total_count": 1, "jobs": [{
    "id": 123892862167, "run_id": 41003441463, "run_attempt": 1,
    "name": "tests/hermes_state (macOS, mini)", "status": "completed",
    "conclusion": "failure", "runner_name": "mini-hermes-state-1",
    "labels": ["self-hosted", "macOS", "hermes-state"],
    "steps": [
        {"number": 1, "name": "Set up job", "status": "completed", "conclusion": "success"},
        {"number": 2, "name": "Checkout code", "status": "completed", "conclusion": "success"},
        {"number": 3, "name": "Run tests/hermes_state", "status": "in_progress", "conclusion": None},
        {"number": 6, "name": "Post Checkout code", "status": "pending", "conclusion": None},
    ],
}]}
RUNNER_LOSS_MESSAGE = (
    "The self-hosted runner lost communication with the server. Verify the machine is "
    "running and has a healthy network connection. Anything in your workflow that "
    "terminates the runner process, starves it for CPU/Memory, or blocks its network "
    "access can cause this error."
)
RUNNER_LOSS_ANNOTATIONS = [{
    "path": ".github", "start_line": 1, "end_line": 1, "annotation_level": "failure",
    "title": "", "message": RUNNER_LOSS_MESSAGE, "raw_details": "",
}]


class FakeApi:
    """In-memory stand-in for the gh client: GET by exact path, records POSTs."""

    def __init__(self, routes):
        self.routes = routes
        self.gets: list[str] = []
        self.posts: list[str] = []

    def get(self, path):
        self.gets.append(path)
        value = self.routes[path]
        if callable(value):
            value = value()
        return copy.deepcopy(value)

    def post(self, path):
        self.posts.append(path)
        return {}


def _event(run):
    return {"action": "completed", "workflow_run": copy.deepcopy(run)}


def _routes(run, jobs, annotations):
    rid = run["id"]
    routes = {
        f"repos/{REPO}/actions/runs/{rid}": run,
        f"repos/{REPO}/actions/runs/{rid}/attempts/1/jobs?per_page=100&page=1": jobs,
        f"repos/{REPO}/actions/runs/{rid}/attempts/1/jobs?per_page=100&page=2": {
            "total_count": jobs["total_count"], "jobs": []},
    }
    routes[f"repos/{REPO}/pulls/123"] = {"state": "open", "head": {"sha": run["head_sha"]}}
    for job_id, anns in annotations.items():
        routes[f"repos/{REPO}/check-runs/{job_id}/annotations?per_page=100&page=1"] = anns
        routes[f"repos/{REPO}/check-runs/{job_id}/annotations?per_page=100&page=2"] = []
    return routes


def test_historical_runner_loss_reruns_failed_jobs_once():
    api = FakeApi(_routes(RUNNER_LOSS_RUN, RUNNER_LOSS_JOBS,
                          {123892862167: RUNNER_LOSS_ANNOTATIONS}))
    decision = recover.recover(_event(RUNNER_LOSS_RUN), api, repository=REPO)
    assert decision.action == "rerun", decision.reason
    assert api.posts == [f"repos/{REPO}/actions/runs/41003441463/rerun-failed-jobs"]


# The mini's agent and the three Softmax room minis' bbci daemons (listed here,
# not read from the module, so an emptied allowlist cannot pass vacuously).
DEDICATED_RUNNERS = ["mini-hermes-state-1", "sx-main-room-mini-hermes",
                     "sx-heart-room-mini-hermes", "sx-conway-mini-hermes"]


@pytest.mark.parametrize("runner", DEDICATED_RUNNERS)
def test_runner_loss_on_each_dedicated_runner_reruns_once(runner):
    run, api = _loss(job_changes={"runner_name": runner})
    decision = recover.recover(_event(run), api, repository=REPO)
    assert decision.action == "rerun", decision.reason
    assert f"runner lost on {runner};" in decision.reason
    assert api.posts == [f"repos/{REPO}/actions/runs/{run['id']}/rerun-failed-jobs"]


# --- run 40751299089: ordinary test failure (real, historical) ---------------------
TEST_FAILURE_RUN = {
    "id": 40751299089, "name": "hermes_state (macOS)",
    "path": ".github/workflows/hermes-state-macos.yml", "event": "push",
    "status": "completed", "conclusion": "failure", "run_attempt": 1,
    "head_sha": "a24e27850bf64e8e04a562620de1bd051e4d194f",
    "repository": _REPO_OBJ, "head_repository": _REPO_OBJ,
}
TEST_FAILURE_JOBS = {"total_count": 1, "jobs": [{
    "id": 123038389129, "run_id": 40751299089, "run_attempt": 1,
    "name": "tests/hermes_state (macOS, mini)", "status": "completed",
    "conclusion": "failure", "runner_name": "mini-hermes-state-1",
    "labels": ["self-hosted", "macOS", "hermes-state"],
    "steps": [
        {"number": 1, "name": "Set up job", "status": "completed", "conclusion": "success"},
        {"number": 2, "name": "Checkout code", "status": "completed", "conclusion": "success"},
        {"number": 3, "name": "Run tests/hermes_state", "status": "completed", "conclusion": "failure"},
        {"number": 6, "name": "Post Checkout code", "status": "completed", "conclusion": "success"},
        {"number": 7, "name": "Complete job", "status": "completed", "conclusion": "success"},
    ],
}]}
TEST_FAILURE_ANNOTATIONS = [{
    "path": ".github", "start_line": 893, "end_line": 893, "annotation_level": "failure",
    "title": "", "message": "Process completed with exit code 1.", "raw_details": "",
}]


def _loss(run_changes=None, job_changes=None, annotations=None, extra_jobs=()):
    """The real runner-loss fixtures, optionally altered for one negative case."""
    run = {**RUNNER_LOSS_RUN, **(run_changes or {})}
    jobs = copy.deepcopy(RUNNER_LOSS_JOBS)
    jobs["jobs"][0].update(job_changes or {})
    jobs["jobs"].extend(copy.deepcopy(list(extra_jobs)))
    jobs["total_count"] = len(jobs["jobs"])
    anns = RUNNER_LOSS_ANNOTATIONS if annotations is None else annotations
    return run, FakeApi(_routes(run, jobs, {jobs["jobs"][0]["id"]: anns}))


def _ci_run_with_loss():
    """CI-run variant (DOCUMENTED FIXTURE, not a captured CI run: no CI-run runner
    loss exists in history). The macOS job is the real 41003441463 job payload under
    ci.yaml's reusable-call name; the two aggregates fail because it failed."""
    run = {**RUNNER_LOSS_RUN, "id": 42000000001, "name": "CI", "event": "pull_request",
           "path": ".github/workflows/ci.yaml", "pull_requests": [{"number": 123}]}
    mac = copy.deepcopy(RUNNER_LOSS_JOBS["jobs"][0])
    mac.update(id=900001, run_id=run["id"],
               name="hermes_state (macOS) / tests/hermes_state (macOS, mini)")
    jobs = [
        {"id": 900000, "run_attempt": 1, "name": "Detect changes", "status": "completed",
         "conclusion": "success", "runner_name": "hub-1", "steps": []},
        mac,
        {"id": 900002, "run_attempt": 1, "name": "macOS state required (v1)", "status": "completed",
         "conclusion": "failure", "runner_name": "hub-2", "steps": []},
        {"id": 900003, "run_attempt": 1, "name": "All required checks pass (v2)", "status": "completed",
         "conclusion": "failure", "runner_name": "hub-3", "steps": []},
        {"id": 900004, "run_attempt": 1, "name": "Desktop E2E", "status": "completed",
         "conclusion": "skipped", "runner_name": None, "steps": []},
    ]
    return run, jobs


def _ci_api(run, jobs):
    return FakeApi(_routes(run, {"total_count": len(jobs), "jobs": jobs},
                           {900001: RUNNER_LOSS_ANNOTATIONS}))


# --- decisions -------------------------------------------------------------------

def test_historical_test_failure_is_never_rerun():
    api = FakeApi(_routes(TEST_FAILURE_RUN, TEST_FAILURE_JOBS,
                          {123038389129: TEST_FAILURE_ANNOTATIONS}))
    decision = recover.recover(_event(TEST_FAILURE_RUN), api, repository=REPO)
    assert decision.action == "skip"
    assert "step(s) failed" in decision.reason
    assert api.posts == []


def test_exit_code_annotation_alone_is_not_a_runner_loss():
    # Even without a failed step, an assertion-style annotation blocks the rerun.
    run, api = _loss(annotations=RUNNER_LOSS_ANNOTATIONS + TEST_FAILURE_ANNOTATIONS)
    decision = recover.recover(_event(run), api, repository=REPO)
    assert decision.action == "skip" and "not a runner loss" in decision.reason
    assert api.posts == []


@pytest.mark.parametrize("message", [
    "The self-hosted runner lost communication with the server",
    "The self-hosted runner lost communication with the server.",
    RUNNER_LOSS_MESSAGE + " ",
    RUNNER_LOSS_MESSAGE.lower(),
])
def test_runner_loss_message_must_match_exactly(message):
    run, api = _loss(annotations=[{**RUNNER_LOSS_ANNOTATIONS[0], "message": message}])
    assert recover.recover(_event(run), api, repository=REPO).action == "skip"
    assert api.posts == []


def test_no_failure_annotation_is_not_a_runner_loss():
    run, api = _loss(annotations=[{**RUNNER_LOSS_ANNOTATIONS[0], "annotation_level": "warning"}])
    decision = recover.recover(_event(run), api, repository=REPO)
    assert decision.action == "skip" and "no failure annotation" in decision.reason
    assert api.posts == []


@pytest.mark.parametrize("run_changes,fragment", [
    ({"path": ".github/workflows/tests.yml"}, "not approved"),
    ({"path": ".github/workflows/ci.yaml@refs/heads/evil"}, "not approved"),
    ({"head_repository": {"full_name": "attacker/hermes"}}, "head repository"),
    ({"head_repository": None}, "head repository"),
    ({"repository": {"full_name": "attacker/hermes"}}, "repository"),
    ({"run_attempt": 2}, "attempt"),
    ({"conclusion": "cancelled"}, "conclusion"),
    ({"conclusion": "success"}, "conclusion"),
    ({"status": "in_progress"}, "status"),
])
def test_event_gates_skip_before_any_api_call(run_changes, fragment):
    run, api = _loss(run_changes=run_changes)
    decision = recover.recover(_event(run), api, repository=REPO)
    assert decision.action == "skip" and fragment in decision.reason
    assert api.gets == [] and api.posts == []


@pytest.mark.parametrize("bad_id", ["41003441463", "1;rm -rf /", True, -1, None, 1.5])
def test_non_integer_run_id_never_reaches_the_api(bad_id):
    run, api = _loss()
    event = _event(run)
    event["workflow_run"]["id"] = bad_id
    assert recover.recover(event, api, repository=REPO).action == "skip"
    assert api.gets == [] and api.posts == []


def test_untrusted_repository_context_is_refused():
    run, api = _loss()
    for repository in ("attacker/hermes", "", "daveey/hermes-fork"):
        assert recover.recover(_event(run), api, repository=repository).action == "skip"
    assert api.gets == [] and api.posts == []


def test_non_completed_event_action_is_skipped():
    run, api = _loss()
    event = {**_event(run), "action": "requested"}
    assert recover.recover(event, api, repository=REPO).action == "skip"
    assert api.gets == []


def test_api_state_overrides_a_stale_or_forged_event():
    # Event says attempt 1 same-repo failure; the API says the head repo is a fork.
    run, api = _loss()
    api.routes[f"repos/{REPO}/actions/runs/{run['id']}"] = {
        **run, "head_repository": {"full_name": "attacker/hermes"}}
    decision = recover.recover(_event(run), api, repository=REPO)
    assert decision.action == "skip" and decision.reason.startswith("api:")
    assert api.posts == []


@pytest.mark.parametrize("job_changes,fragment", [
    ({"runner_name": "mini-hermes-state-2"}, "ran on"),
    ({"runner_name": None}, "ran on"),
    ({"runner_name": "sx-main-room-mini"}, "ran on"),
    ({"runner_name": ["sx-main-room-mini-hermes"]}, "ran on"),
    ({"name": "tests/hermes_state (macOS, mini) "}, "expected exactly one"),
    ({"name": "hermes_state (macOS) / tests/hermes_state (macOS, mini)"}, "expected exactly one"),
    ({"conclusion": "cancelled"}, "not completed/failure"),
    ({"conclusion": "timed_out"}, "not completed/failure"),
    ({"run_attempt": 2}, "attempt"),
])
def test_macos_job_gates(job_changes, fragment):
    run, api = _loss(job_changes=job_changes)
    decision = recover.recover(_event(run), api, repository=REPO)
    assert decision.action == "skip" and fragment in decision.reason, decision.reason
    assert api.posts == []


def test_duplicate_macos_job_name_is_refused():
    dup = {**RUNNER_LOSS_JOBS["jobs"][0], "id": 1}
    run, api = _loss(extra_jobs=[dup])
    decision = recover.recover(_event(run), api, repository=REPO)
    assert decision.action == "skip" and "found 2" in decision.reason
    assert api.posts == []


def test_ci_reusable_runner_loss_reruns_with_its_aggregates():
    run, jobs = _ci_run_with_loss()
    api = _ci_api(run, jobs)
    decision = recover.recover(_event(run), api, repository=REPO)
    assert decision.action == "rerun", decision.reason
    assert api.posts == [f"repos/{REPO}/actions/runs/{run['id']}/rerun-failed-jobs"]


@pytest.mark.parametrize("state,sha", [("open", "new-head"), ("closed", RUNNER_LOSS_RUN["head_sha"])])
def test_obsolete_pr_recovery_never_cancels_current_ci(state, sha):
    run, jobs = _ci_run_with_loss()
    api = _ci_api(run, jobs)
    api.routes[f"repos/{REPO}/pulls/123"] = {"state": state, "head": {"sha": sha}}
    assert recover.recover(_event(run), api, repository=REPO).action == "skip"
    assert api.posts == []


@pytest.mark.parametrize("pulls", [[], [{"number": 1}, {"number": 2}], [{}]])
def test_unknown_pr_identity_fails_closed(pulls):
    run, jobs = _ci_run_with_loss()
    run["pull_requests"] = pulls
    api = _ci_api(run, jobs)
    assert recover.recover(_event(run), api, repository=REPO).action == "skip"
    assert api.posts == []


def test_ci_standalone_job_name_is_not_the_ci_lane():
    run, jobs = _ci_run_with_loss()
    jobs[1]["name"] = "tests/hermes_state (macOS, mini)"
    api = _ci_api(run, jobs)
    assert recover.recover(_event(run), api, repository=REPO).action == "skip"
    assert api.posts == []


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
def test_any_other_failed_job_blocks_rerun_failed_jobs(conclusion):
    # rerun-failed-jobs would rerun this one too: never rerun an unrelated failure.
    run, jobs = _ci_run_with_loss()
    jobs.append({"id": 900005, "run_attempt": 1, "name": "Tests / test (3.13)",
                 "status": "completed", "conclusion": conclusion, "runner_name": "hub-4",
                 "steps": []})
    api = _ci_api(run, jobs)
    decision = recover.recover(_event(run), api, repository=REPO)
    assert decision.action == "skip" and "Tests / test (3.13)" in decision.reason
    assert api.posts == []


def test_aggregate_names_are_not_downstream_of_the_standalone_workflow():
    agg = {"id": 7, "run_attempt": 1, "name": "All required checks pass (v2)", "status": "completed",
           "conclusion": "failure", "runner_name": "hub-1", "steps": []}
    run, api = _loss(extra_jobs=[agg])
    assert recover.recover(_event(run), api, repository=REPO).action == "skip"
    assert api.posts == []


def test_jobs_are_read_for_attempt_one_across_pages():
    run, jobs = _ci_run_with_loss()
    filler = [{"id": 800000 + i, "run_attempt": 1, "name": f"Lane {i}", "status": "completed",
               "conclusion": "success", "runner_name": "hub", "steps": []} for i in range(100)]
    total = len(filler) + len(jobs)
    api = _ci_api(run, filler)
    base = f"repos/{REPO}/actions/runs/{run['id']}/attempts/1/jobs?per_page=100"
    api.routes[f"{base}&page=1"] = {"total_count": total, "jobs": filler}
    api.routes[f"{base}&page=2"] = {"total_count": total, "jobs": jobs}
    decision = recover.recover(_event(run), api, repository=REPO)
    assert decision.action == "rerun", decision.reason
    assert f"{base}&page=2" in api.gets
    assert all("/attempts/1/jobs" in g for g in api.gets if "/jobs" in g)


def test_failed_job_on_a_later_page_still_blocks():
    run, api = _loss()
    base = f"repos/{REPO}/actions/runs/{run['id']}/attempts/1/jobs?per_page=100"
    first = copy.deepcopy(RUNNER_LOSS_JOBS["jobs"])
    first += [{"id": 700000 + i, "run_attempt": 1, "name": f"x{i}", "status": "completed",
               "conclusion": "success", "steps": []} for i in range(99)]
    api.routes[f"{base}&page=1"] = {"total_count": 101, "jobs": first}
    api.routes[f"{base}&page=2"] = {"total_count": 101, "jobs": [
        {"id": 1, "run_attempt": 1, "name": "late", "status": "completed",
         "conclusion": "failure", "steps": []}]}
    decision = recover.recover(_event(run), api, repository=REPO)
    assert decision.action == "skip" and "late" in decision.reason
    assert api.posts == []


def test_reread_before_mutation_sees_a_new_attempt():
    run, api = _loss()
    path = f"repos/{REPO}/actions/runs/{run['id']}"
    reads = iter([run, {**run, "run_attempt": 2, "status": "queued", "conclusion": None}])
    api.routes[path] = lambda: next(reads)
    decision = recover.recover(_event(run), api, repository=REPO)
    assert decision.action == "skip" and decision.reason.startswith("re-read before rerun")
    assert api.gets.count(path) == 2
    assert api.posts == []


def test_second_event_for_the_same_run_does_not_rerun_again():
    """Serialized (cancel-in-progress: false) duplicate delivery: the first event's
    POST starts attempt 2, so the second event's API read sees it and stops."""
    run, api = _loss()
    path = f"repos/{REPO}/actions/runs/{run['id']}"
    state = {"run": dict(run)}
    api.routes[path] = lambda: state["run"]
    real_post = api.post

    def post(path_):
        state["run"] = {**run, "run_attempt": 2, "status": "queued", "conclusion": None}
        return real_post(path_)

    api.post = post  # type: ignore[method-assign]
    first = recover.recover(_event(run), api, repository=REPO)
    second = recover.recover(_event(run), api, repository=REPO)
    assert (first.action, second.action) == ("rerun", "skip")
    assert "attempt is 2" in second.reason
    assert api.posts == [f"{path}/rerun-failed-jobs"]


def test_dry_run_decides_without_posting():
    run, api = _loss()
    decision = recover.recover(_event(run), api, repository=REPO, dry_run=True)
    assert (decision.action, decision.dry_run) == ("rerun", True)
    assert api.posts == []


# --- CLI and gh client -----------------------------------------------------------

def test_main_reads_event_file_and_prints_decision(tmp_path, monkeypatch, capsys):
    run, api = _loss()
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(_event(run)), encoding="utf-8")
    summary = tmp_path / "summary.md"
    monkeypatch.setattr(recover, "GhApi", lambda: api)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert recover.main(["--event", str(event_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "rerun" and out["run_id"] == run["id"]
    assert api.posts == [f"repos/{REPO}/actions/runs/{run['id']}/rerun-failed-jobs"]
    assert "rerun" in summary.read_text(encoding="utf-8")


def test_main_skip_is_success_and_api_error_is_failure(tmp_path, monkeypatch, capsys):
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(_event(TEST_FAILURE_RUN)), encoding="utf-8")
    api = FakeApi(_routes(TEST_FAILURE_RUN, TEST_FAILURE_JOBS,
                          {123038389129: TEST_FAILURE_ANNOTATIONS}))
    monkeypatch.setattr(recover, "GhApi", lambda: api)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert recover.main(["--event", str(event_path), "--repository", REPO]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "skip"

    class Broken(FakeApi):
        def get(self, path):
            raise recover.ApiError("HTTP 502")

    monkeypatch.setattr(recover, "GhApi", lambda: Broken({}))
    assert recover.main(["--event", str(event_path), "--repository", REPO]) == 1
    assert api.posts == []


def test_gh_client_uses_an_argv_without_a_shell(monkeypatch):
    calls = []

    class Done:
        returncode = 0
        stdout = '{"ok": true}'
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Done()

    monkeypatch.setattr(recover.subprocess, "run", fake_run)
    client = recover.GhApi()
    assert client.get("repos/softmax-universe/daveey-hermes/actions/runs/1") == {"ok": True}
    client.post("repos/softmax-universe/daveey-hermes/actions/runs/1/rerun-failed-jobs")
    (get_argv, get_kw), (post_argv, post_kw) = calls
    assert get_argv[:2] == ["gh", "api"] and get_argv[-1] == "repos/softmax-universe/daveey-hermes/actions/runs/1"
    assert post_argv[:4] == ["gh", "api", "-X", "POST"]
    assert post_argv[-1] == "repos/softmax-universe/daveey-hermes/actions/runs/1/rerun-failed-jobs"
    assert not get_kw.get("shell") and not post_kw.get("shell")


def test_gh_client_error_raises(monkeypatch):
    class Failed:
        returncode = 1
        stdout = ""
        stderr = "HTTP 403"

    monkeypatch.setattr(recover.subprocess, "run", lambda argv, **kw: Failed())
    with pytest.raises(recover.ApiError, match="403"):
        recover.GhApi().get("repos/softmax-universe/daveey-hermes/actions/runs/1")


# --- workflow wiring -------------------------------------------------------------

def _wf(path):
    return _YAML.load(path.read_text(encoding="utf-8"))


def test_recovery_listens_to_exactly_the_approved_workflows():
    trigger = _wf(RECOVERY_WORKFLOW)["on"]
    assert set(trigger) == {"workflow_run"}
    assert trigger["workflow_run"]["types"] == ["completed"]
    names = {_wf(WORKFLOWS / p.rsplit("/", 1)[1])["name"] for p in recover.WORKFLOWS}
    assert set(trigger["workflow_run"]["workflows"]) == names == {"CI", "hermes_state (macOS)"}


def test_job_names_match_the_real_workflow_files():
    mac = _wf(WORKFLOWS / "hermes-state-macos.yml")["jobs"]["hermes-state-macos"]
    assert mac["name"] == recover.MACOS_JOB
    ci_jobs = _wf(WORKFLOWS / "ci.yaml")["jobs"]
    caller = ci_jobs["hermes-state-macos"]
    assert caller["uses"] == "./.github/workflows/hermes-state-macos.yml"
    ci_spec = recover.WORKFLOWS[".github/workflows/ci.yaml"]
    assert ci_spec["job"] == f"{caller['name']} / {mac['name']}"
    downstream = {j["name"] for j in ci_jobs.values()
                  if "hermes-state-macos" in (j.get("needs") or []) and "name" in j}
    assert set(ci_spec["downstream"]) == downstream


def test_recovery_permissions_and_trusted_checkout():
    wf = _wf(RECOVERY_WORKFLOW)
    assert wf["permissions"] == {}
    (job,) = wf["jobs"].values()
    assert job["permissions"] == {"actions": "write", "checks": "read", "contents": "read"}
    checkout = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkout) == 1
    assert checkout[0]["with"]["ref"] == "${{ github.workflow_sha }}"
    assert checkout[0]["with"]["persist-credentials"] is False
    text = RECOVERY_WORKFLOW.read_text(encoding="utf-8")
    for forbidden in ("download-artifact", "head_sha", "head_branch", "pull_request_target",
                      "secrets."):
        assert forbidden not in text


def test_no_payload_interpolation_into_a_shell():
    (job,) = _wf(RECOVERY_WORKFLOW)["jobs"].values()
    runs = [s["run"] for s in job["steps"] if "run" in s]
    assert runs == ['python3 scripts/ci/recover_macos_runner.py --event "$GITHUB_EVENT_PATH"']
    for step in job["steps"]:
        for value in (step.get("env") or {}).values():
            assert "github.event" not in str(value)


def test_recovery_concurrency_is_per_run_and_serialized():
    from tests.ci import _gha_expr as gha

    conc = _wf(RECOVERY_WORKFLOW)["concurrency"]
    assert conc["cancel-in-progress"] is False

    def group(rid):
        return gha.render(conc["group"], {"github": {"event": {"workflow_run": {"id": rid}}}})

    assert group(1) == group(1) != group(2)


def test_job_prefilter_requires_trusted_first_attempt_failure():
    (job,) = _wf(RECOVERY_WORKFLOW)["jobs"].values()
    cond = " ".join(job["if"].split())
    for clause in ("github.repository == 'softmax-universe/daveey-hermes'",
                   "github.event.workflow_run.conclusion == 'failure'",
                   "github.event.workflow_run.run_attempt == 1",
                   "github.event.workflow_run.head_repository.full_name == 'softmax-universe/daveey-hermes'"):
        assert clause in cond
