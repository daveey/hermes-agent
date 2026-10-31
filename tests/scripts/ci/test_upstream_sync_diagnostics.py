"""REFUSED diagnostics for a red upstream sync: failing jobs and tests, main-baseline vs candidate-only, the nightly
owner. Diagnostics are evidence only: they never change the gate's verdict and never blame upstream for a failure
main already has. GitHub's API is a fixture; the job logs are real pytest summary line shapes."""
import json
import subprocess

import pytest

from scripts.ci import upstream_sync as sync
from scripts.ci import upstream_sync_diagnostics as diag
from scripts.ci.nightly_e2e import failure_key

BASE = "b" * 40
OTHER = "c" * 40
A = "tests/e2e/core/test_session.py::test_resume"
B = "tests/e2e/core/test_cron.py::test_tick[la]"
PY = "tests/agent/test_turn.py::test_retry"


def log(*ids, error=()):
    lines = [f"2026-10-30T16:48:05.3882424Z FAILED {i} - AssertionError: x" for i in ids]
    lines += [f"2026-10-30T16:48:05.3882424Z ERROR {i}" for i in error]
    return "\n".join(["2026-10-30T16:48:05Z === Summary: 120 files, 1 failed ===", *lines])


def job(job_id, name, conclusion):
    return {"id": job_id, "name": name, "status": "completed", "conclusion": conclusion,
            "html_url": f"https://ci.example/job/{job_id}"}


def candidate_jobs(e2e="failure", py_slice=None):
    jobs = [job(21, "Full suite / e2e", e2e)]
    for n in range(1, 9):
        jobs.append(job(30 + n, f"Full suite / Run tests ({n}/8)", "failure" if n == py_slice else "success"))
    return jobs


def nightly_run(run_id=50, path=".github/workflows/nightly-e2e.yml", event="workflow_dispatch"):
    return {"id": run_id, "head_sha": "workflow-commit", "head_branch": "main", "event": event, "path": path,
            "status": "completed", "html_url": f"https://ci.example/run/{run_id}"}


def ci_run(run_id=60, sha=BASE, branch="main", event="push"):
    return {"id": run_id, "head_sha": sha, "head_branch": branch, "event": event, "path": ".github/workflows/ci.yaml",
            "status": "completed", "html_url": f"https://ci.example/run/{run_id}"}


class FakeGitHub:
    """runs: ci.yaml runs at the base sha; nightlies: nightly-e2e.yml runs; jobs/logs keyed by run/job id."""

    def __init__(self, *, cand_jobs=None, ci_runs=(), nightlies=(), jobs=None, logs=None):
        self.ci_runs, self.nightlies = list(ci_runs), list(nightlies)
        self.jobs = {20: cand_jobs if cand_jobs is not None else candidate_jobs(), **(jobs or {})}
        self.logs = {21: log(A), **(logs or {})}

    def request(self, endpoint):
        if "/jobs?" in endpoint:
            run_id = int(endpoint.split("/runs/", 1)[1].split("/", 1)[0])
            return [{"jobs": self.jobs.get(run_id, [])}]
        if f"runs?head_sha={BASE}" in endpoint:
            return [{"workflow_runs": self.ci_runs}]
        if "workflows/nightly-e2e.yml/runs" in endpoint:
            return [{"workflow_runs": self.nightlies}]
        raise AssertionError(endpoint)

    def gh(self, *args, reason=None):
        job_id = int(args[1].split("/jobs/", 1)[1].split("/", 1)[0])
        text = self.logs.get(job_id)
        if text is None:
            return subprocess.CompletedProcess(args, 1, "", "HTTP 410: logs expired")
        return subprocess.CompletedProcess(args, 0, text, "")


def nightly_at(base, e2e_conclusion, e2e_log, run_id=50):
    """A nightly run that verifiably tested ``base`` (its Resolve ref log), with its e2e verdict and log."""
    return dict(
        nightlies=[nightly_run(run_id)],
        jobs={run_id: [job(run_id + 1, "Resolve ref", "success"), job(run_id + 2, "e2e / e2e", e2e_conclusion),
                       job(run_id + 3, "e2e / e2e-upgrade-plan", "skipped")]},
        logs={run_id + 1: f"2026-10-31T00:42:42.5418698Z Testing main = {base}\n", run_id + 2: e2e_log},
    )


def explain(github, tmp_path, monkeypatch, capsys, owner=""):
    monkeypatch.setattr(diag, "nightly_owner", lambda ids, **kw: owner)
    monkeypatch.setattr(diag, "DEFAULT_STATE", tmp_path / "nightly-e2e.json")
    (tmp_path / "nightly-e2e.json").write_text("{}")
    cand = sync.Candidate("a" * 40, "red", "https://ci.example/run/20", 0, run_id=20)
    diag.explain(tmp_path, cand, "main", "example/fork", github.request, github.gh,
                 required=sync.REQUIRED_JOBS, git=lambda *a, **k: BASE)
    return capsys.readouterr().out


def test_main_baseline_failure_names_job_tests_owner_and_never_blames_upstream(tmp_path, monkeypatch, capsys):
    github = FakeGitHub(**nightly_at(BASE, "failure", log(A)))
    out = explain(github, tmp_path, monkeypatch, capsys, owner="t_owner (running)")
    assert "failed job: Full suite / e2e (failure) https://ci.example/job/21" in out and A in out
    assert f"main-baseline failure: 1 failing test(s) also fail on main {BASE[:10]} (https://ci.example/run/50)" in out
    assert "not evidence that upstream caused them" in out
    assert "candidate-only" not in out
    assert "nightly-e2e owner for this exact failing set: t_owner (running)" in out


def test_candidate_only_needs_a_completed_passing_job_at_the_same_base(tmp_path, monkeypatch, capsys):
    out = explain(FakeGitHub(**nightly_at(BASE, "success", "")), tmp_path, monkeypatch, capsys)
    assert f"candidate-only failure observed: 1 test(s) did not fail on main {BASE[:10]}" in out
    assert "not proof of upstream causation" in out and "main-baseline" not in out
    assert "no open nightly-e2e card matches" in out


def test_partial_overlap_splits_preexisting_from_candidate_only(tmp_path, monkeypatch, capsys):
    github = FakeGitHub(**nightly_at(BASE, "failure", log(A)), )
    github.logs[21] = log(A, B)
    out = explain(github, tmp_path, monkeypatch, capsys)
    baseline_line = next(line for line in out.split("  e2e: ") if line.startswith("main-baseline"))
    only_line = next(line for line in out.split("  e2e: ") if line.startswith("candidate-only"))
    assert A in baseline_line and B not in baseline_line
    assert B in only_line and A not in only_line


def test_python_slice_compares_by_test_id_against_main_ci_yaml(tmp_path, monkeypatch, capsys):
    slices = [job(70 + n, f"Python tests / Run tests ({n}/8)", "success") for n in range(1, 9)]
    github = FakeGitHub(cand_jobs=candidate_jobs(e2e="success", py_slice=3), ci_runs=[ci_run()],
                        jobs={60: [*slices, job(80, "Python tests / e2e", "skipped")]}, logs={33: log(PY)})
    out = explain(github, tmp_path, monkeypatch, capsys)
    assert "failed job: Full suite / Run tests (3/8) (failure)" in out and PY in out
    assert "python: candidate-only failure observed" in out and "https://ci.example/run/60" in out
    assert "nightly" not in out  # no e2e failure: no owner lookup


def _nightly_green():
    return FakeGitHub(**nightly_at(BASE, "success", ""))


def _with(github, **changes):
    for name, value in changes.items():
        target = getattr(github, name)
        if isinstance(target, dict):
            target.update(value)
        else:
            setattr(github, name, value)
    return github


def _main_ci(branch="main", event="push", e2e="success"):
    return _with(_nightly_green(), nightlies=[], ci_runs=[ci_run(branch=branch, event=event)],
                 jobs={60: [job(61, "Python tests / e2e", e2e)]})


UNKNOWN_EVIDENCE = {
    "candidate_log_unavailable": lambda: _with(_nightly_green(), logs={21: None}),
    "no_baseline_run": lambda: _with(_nightly_green(), nightlies=[]),
    "nightly_tested_another_sha": lambda: _with(_nightly_green(), logs={51: f"2026-10-31T00:42:42Z Testing main = {OTHER}\n"}),
    "nightly_resolve_log_unavailable": lambda: _with(_nightly_green(), logs={51: None}),
    "ci_run_off_main": lambda: _main_ci(branch="feature"),
    "ci_run_pull_request": lambda: _main_ci(event="pull_request"),
    "baseline_e2e_skipped": lambda: _main_ci(e2e="skipped"),
    "baseline_log_unavailable": lambda: FakeGitHub(**nightly_at(BASE, "failure", None)),
    "python_baseline_affected_only": lambda: FakeGitHub(
        cand_jobs=candidate_jobs(e2e="success", py_slice=3), ci_runs=[ci_run()],
        jobs={60: [job(71, "Python tests / Run tests (affected, 1/2)", "success")]}, logs={33: log(PY)}),
}


@pytest.mark.parametrize("case", sorted(UNKNOWN_EVIDENCE))
def test_missing_or_incomparable_evidence_is_unknown_not_attributed(tmp_path, monkeypatch, capsys, case):
    out = explain(UNKNOWN_EVIDENCE[case](), tmp_path, monkeypatch, capsys)
    assert "baseline comparison unknown" in out
    assert "main-baseline failure" not in out and "candidate-only failure observed" not in out


def test_unparsed_candidate_failure_states_baseline_fact_without_attribution(tmp_path, monkeypatch, capsys):
    github = FakeGitHub(**nightly_at(BASE, "failure", log(A)))
    github.logs[21] = "2026-10-30T16:48:05Z Error: the job ran out of disk\n"
    out = explain(github, tmp_path, monkeypatch, capsys)
    assert "no failing test id parsed" in out
    assert "baseline comparison unknown for a failed job with no parsed test ids" in out and "also failed" in out
    assert "candidate-only" not in out


def test_owner_lookup_failure_keeps_the_ci_evidence(tmp_path, monkeypatch, capsys):
    github = FakeGitHub(**nightly_at(BASE, "failure", log(A)))
    monkeypatch.setattr(diag, "DEFAULT_STATE", tmp_path / "nightly-e2e.json")
    (tmp_path / "nightly-e2e.json").write_text(json.dumps({"cards": {failure_key([A]): {"id": "t_owner", "n": 1}}}))

    class OfflineBoard(diag.NightlyBoard):
        def _run(self, *args, **kwargs):
            raise RuntimeError("hermes kanban: board offline")

    monkeypatch.setattr(diag, "NightlyBoard", OfflineBoard)
    cand = sync.Candidate("a" * 40, "red", "https://ci.example/run/20", 0, run_id=20)
    diag.explain(tmp_path, cand, "main", "example/fork", github.request, github.gh,
                 required=sync.REQUIRED_JOBS, git=lambda *a, **k: BASE)
    out = capsys.readouterr().out
    assert "main-baseline failure" in out
    assert "nightly owner lookup unavailable" in out and "unreadable" in out


def test_no_nightly_state_on_this_host_is_unavailable_not_no_owner(tmp_path, monkeypatch, capsys):
    github = FakeGitHub(**nightly_at(BASE, "failure", log(A)))
    monkeypatch.setattr(diag, "DEFAULT_STATE", tmp_path / "absent.json")
    cand = sync.Candidate("a" * 40, "red", "https://ci.example/run/20", 0, run_id=20)
    diag.explain(tmp_path, cand, "main", "example/fork", github.request, github.gh,
                 required=sync.REQUIRED_JOBS, git=lambda *a, **k: BASE)
    out = capsys.readouterr().out
    assert "nightly owner lookup unavailable: no nightly-e2e state on this host" in out
    assert "no open nightly-e2e card matches" not in out


@pytest.mark.parametrize("status,creator,key_matches,expected", [
    ("running", "nightly-e2e", True, True), ("blocked", "nightly-e2e", True, True),
    ("done", "nightly-e2e", True, False), ("archived", "nightly-e2e", True, False),
    ("running", "default", True, False), ("running", "nightly-e2e", False, False),
])
def test_owner_requires_open_nightly_provenance_and_the_exact_fingerprint(tmp_path, status, creator, key_matches,
                                                                           expected):
    key = failure_key([A, B])
    state = tmp_path / "nightly.json"
    state.write_text(json.dumps({"cards": {key: {"id": "t_owner", "n": 1}}}))

    class Board:
        def card(self, card_id):
            assert card_id == "t_owner"
            return {"id": card_id, "status": status, "created_by": creator,
                    "idempotency_key": f"nightly-e2e:{key if key_matches else 'failed-000000000000'}:1"}

    assert bool(diag.nightly_owner([B, A], state_path=state, board=Board())) is expected
    assert diag.nightly_owner([A], state_path=state, board=Board()) == ""  # a subset is a different set
    assert diag.nightly_owner([], state_path=state, board=Board()) == ""


def gate_world(monkeypatch, tmp_path):
    monkeypatch.setattr(diag, "DEFAULT_STATE", tmp_path / "nightly-e2e.json")
    (tmp_path / "nightly-e2e.json").write_text("{}")
    monkeypatch.setattr(sync, "git", lambda repo, *args, **kw: BASE if args[0] == "merge-base" else "a" * 40)
    monkeypatch.setattr(sync, "_fetch_main", lambda *a: "main")
    monkeypatch.setattr(sync, "_is_ancestor", lambda *a: False)
    monkeypatch.setattr(sync, "_adds_nothing", lambda *a: False)


def red_runs():
    return [{"workflow_runs": [{"id": 20, "run_number": 1, "head_sha": "a" * 40, "head_branch": sync.BRANCH_PREFIX + "a" * 40,
                                "path": sync.WORKFLOW_PATH, "event": "push", "status": "completed",
                                "conclusion": "failure", "html_url": "https://ci.example/run/20",
                                "updated_at": "2026-10-30T16:48:05Z"}]}]


def test_gate_prints_diagnostics_under_refused_and_still_refuses(tmp_path, monkeypatch, capsys):
    """A red candidate whose failures main already has is still REFUSED: diagnostics never waive the gate."""
    gate_world(monkeypatch, tmp_path)
    github = FakeGitHub(**nightly_at(BASE, "failure", log(A)))
    merges = []

    def request(endpoint):
        return red_runs() if f"workflows/{sync.WORKFLOW_FILE}/runs" in endpoint else github.request(endpoint)

    def gh(*args, reason=None):
        if args[0] == "pr":
            merges.append(args)
        return github.gh(*args, reason=reason)

    assert sync.advance(tmp_path, "a" * 40, gh_repo="example/fork", request=request, gh=gh) == sync.REFUSED
    out = capsys.readouterr().out
    assert out.startswith(f"REFUSED: the full suite failed on {'a' * 10}: https://ci.example/run/20")
    assert "main-baseline failure" in out and A in out
    assert merges == []  # no PR opened or merged


def test_gate_still_refuses_when_diagnostic_reads_fail(tmp_path, monkeypatch, capsys):
    gate_world(monkeypatch, tmp_path)

    def request(endpoint):
        if f"workflows/{sync.WORKFLOW_FILE}/runs" in endpoint:
            return red_runs()
        raise sync.GateError("API offline")

    assert sync.advance(tmp_path, "a" * 40, gh_repo="example/fork", request=request) == sync.REFUSED
    out = capsys.readouterr().out
    assert "REFUSED: the full suite failed" in out and "diagnostics unavailable" in out and "API offline" in out
