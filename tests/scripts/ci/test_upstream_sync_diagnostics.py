"""REFUSED diagnostics for a red upstream sync: failing jobs and tests, main-baseline vs candidate-only, the nightly
owner. Diagnostics are evidence only: they never change the gate's verdict and never blame upstream for a failure
main already has. GitHub's API is a fixture; the job logs are real pytest summary line shapes."""
import json
import subprocess
import sys
from typing import Any

import pytest

from scripts.ci import upstream_sync as sync
from scripts.ci import upstream_sync_diagnostics as diag
from scripts.ci.nightly_e2e import _card_body, failure_key

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


BASE_TIME = 1793318400  # 2026-10-30T00:00:00Z


def fake_git(repo, *args, first_parent_on_main=True, **kw):
    """git for a candidate whose first parent is BASE (on main unless told otherwise)."""
    if args[0] == "rev-parse":
        return BASE if first_parent_on_main else OTHER
    if args[0] == "merge-base":
        return BASE if args[1] == BASE or not first_parent_on_main else OTHER
    if args[0] == "show":
        return str(BASE_TIME)
    raise AssertionError(args)


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
            assert "created=%3E%3D2026-10-30T00:00:00Z" in endpoint  # only nightlies after the base commit
            return [{"workflow_runs": self.nightlies}]
        raise AssertionError(endpoint)

    def gh(self, *args, reason=None):
        job_id = int(args[1].split("/jobs/", 1)[1].split("/", 1)[0])
        text = self.logs.get(job_id)
        if text is None:
            return subprocess.CompletedProcess(args, 1, "", "HTTP 410: logs expired")
        return subprocess.CompletedProcess(args, 0, text, "")


def nightly_at(base, e2e_conclusion, e2e_log, run_id=50) -> dict[str, Any]:
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
                 required=sync.REQUIRED_JOBS, git=fake_git)
    return capsys.readouterr().out


def test_main_baseline_failure_names_job_tests_owner_and_never_blames_upstream(tmp_path, monkeypatch, capsys):
    github = FakeGitHub(**nightly_at(BASE, "failure", log(A)))
    out = explain(github, tmp_path, monkeypatch, capsys, owner="t_owner (running)")
    assert "required job failure: Full suite / e2e https://ci.example/job/21" in out and A in out
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
    assert "required job failure: Full suite / Run tests (3/8)" in out and PY in out
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
    assert "baseline comparison unknown for a required job whose failing tests were not all parsed" in out
    assert "also failed" in out
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
                 required=sync.REQUIRED_JOBS, git=fake_git)
    out = capsys.readouterr().out
    assert "main-baseline failure" in out
    assert "nightly owner lookup unavailable" in out and "unreadable" in out


def test_no_nightly_state_on_this_host_is_unavailable_not_no_owner(tmp_path, monkeypatch, capsys):
    github = FakeGitHub(**nightly_at(BASE, "failure", log(A)))
    monkeypatch.setattr(diag, "DEFAULT_STATE", tmp_path / "absent.json")
    cand = sync.Candidate("a" * 40, "red", "https://ci.example/run/20", 0, run_id=20)
    diag.explain(tmp_path, cand, "main", "example/fork", github.request, github.gh,
                 required=sync.REQUIRED_JOBS, git=fake_git)
    out = capsys.readouterr().out
    assert "nightly owner lookup unavailable: no nightly-e2e state on this host" in out
    assert "no open nightly-e2e card matches" not in out


def card_body(ids):
    """The body the nightly reporter files for this failing set (its real renderer)."""
    entry = {"ref": "main", "sha": BASE, "dispatched_at": BASE_TIME, "url": "https://ci.example/run/50"}
    return _card_body("failed", entry, sorted(ids), "=== Summary: 1 failed ===")


@pytest.mark.parametrize("status,creator,idem,body_ids,expected", [
    ("running", "nightly-e2e", "match", (A, B), True), ("blocked", "nightly-e2e", "match", (A, B), True),
    ("running", "nightly-e2e", None, (A, B), True),  # `kanban show --json` returns no idempotency key
    ("done", "nightly-e2e", "match", (A, B), False), ("archived", "nightly-e2e", "match", (A, B), False),
    ("running", "default", "match", (A, B), False), ("running", "nightly-e2e", "other", (A, B), False),
    ("running", "nightly-e2e", None, (A,), False), ("running", "nightly-e2e", None, (A, B, PY), False),
])
def test_owner_requires_open_nightly_provenance_and_the_exact_fingerprint(tmp_path, status, creator, idem, body_ids,
                                                                           expected):
    key = failure_key([A, B])
    state = tmp_path / "nightly.json"
    state.write_text(json.dumps({"cards": {key: {"id": "t_owner", "n": 1}}}))
    keys = {"match": f"nightly-e2e:{key}:1", "other": "nightly-e2e:failed-000000000000:1"}

    class Board:
        def card(self, card_id):
            assert card_id == "t_owner"
            card = {"id": card_id, "status": status, "created_by": creator, "body": card_body(body_ids)}
            if idem:
                card["idempotency_key"] = keys[idem]
            return card

    assert bool(diag.nightly_owner([B, A], state_path=state, board=Board())) is expected
    assert diag.nightly_owner([A], state_path=state, board=Board()) == ""  # a subset is a different set
    assert diag.nightly_owner([], state_path=state, board=Board()) == ""


@pytest.mark.parametrize("identity", ["nightly-e2e:red:3", None])
@pytest.mark.parametrize("body_ids", [(A, B), (A,)])
def test_red_owner_uses_latest_set_not_initial_body(tmp_path, body_ids, identity):
    state = tmp_path / "nightly.json"
    state.write_text(json.dumps({"cards": {"red": {
        "id": "t_owner", "n": 3, "scope": "main", "failing": [B, A],
    }}}))
    board = StateBoard({"t_owner": {
        "id": "t_owner", "status": "running", "created_by": "nightly-e2e",
        "idempotency_key": identity, "body": card_body(body_ids),
    }})
    assert diag.nightly_owner([A, B], state_path=state, board=board) == "t_owner (running)"
    assert diag.nightly_owner([A], state_path=state, board=board) == ""
    assert diag.nightly_owner([A, B, PY], state_path=state, board=board) == ""


@pytest.mark.parametrize("saved_identity", [False, True])
def test_red_owner_keeps_adopted_legacy_identity(tmp_path, saved_identity):
    identity = f"nightly-e2e:{failure_key([A])}:4"
    rec = {"id": "t_owner", "n": 0, "scope": "main", "failing": [B]}
    if saved_identity:
        rec["idempotency_key"] = identity
    state = tmp_path / "nightly.json"
    state.write_text(json.dumps({"cards": {"red": rec}}))
    board = StateBoard({"t_owner": {
        "id": "t_owner", "status": "running", "created_by": "nightly-e2e",
        "idempotency_key": identity, "body": "edited body" if saved_identity else card_body([A]),
    }})
    assert diag.nightly_owner([B], state_path=state, board=board) == "t_owner (running)"
    assert diag.nightly_owner([A], state_path=state, board=board) == ""


@pytest.mark.parametrize("change", [
    {"created_by": "default"}, {"idempotency_key": "nightly-e2e:timeout:3"},
    {"idempotency_key": "nightly-e2e:red:2"},
    {"idempotency_key": f"nightly-e2e:{failure_key([PY])}:4"},
    {"idempotency_key": "nightly-e2e:acceptance|red:3"}, {"status": "done"},
    {"id": "t_unrelated"},
])
def test_red_owner_rejects_unrelated_provenance(tmp_path, change):
    state = tmp_path / "nightly.json"
    state.write_text(json.dumps({"cards": {"red": {
        "id": "t_owner", "n": 3, "scope": "main", "failing": [B],
    }}}))
    card = {"id": "t_owner", "status": "running", "created_by": "nightly-e2e",
            "idempotency_key": "nightly-e2e:red:3", "body": card_body([A]), **change}
    assert diag.nightly_owner([B], state_path=state, board=StateBoard({"t_owner": card})) == ""


@pytest.mark.parametrize("key", ["red", "acceptance|red", failure_key([B])])
def test_acceptance_record_is_not_main_owner(tmp_path, key):
    state = tmp_path / "nightly.json"
    state.write_text(json.dumps({"cards": {key: {
        "id": "t_owner", "n": 1, "scope": "acceptance", "failing": [B],
    }}}))
    card = {"id": "t_owner", "status": "running", "created_by": "nightly-e2e", "body": card_body([B])}
    assert diag.nightly_owner([B], state_path=state, board=StateBoard({"t_owner": card})) == ""


@pytest.mark.parametrize("saved,actual", [
    (f"nightly-e2e:{failure_key([A])}:4", f"nightly-e2e:{failure_key([A])}:5"),
    ("nightly-e2e:acceptance|red:3", "nightly-e2e:acceptance|red:3"),
    ("nightly-e2e:timeout:3", "nightly-e2e:timeout:3"),
    ("nightly-e2e:red:2", "nightly-e2e:red:2"),
])
def test_saved_red_identity_cannot_authorize_an_unrelated_card(tmp_path, saved, actual):
    state = tmp_path / "nightly.json"
    state.write_text(json.dumps({"cards": {"red": {
        "id": "t_owner", "n": 3, "scope": "main", "failing": [B], "idempotency_key": saved,
    }}}))
    card = {"id": "t_owner", "status": "running", "created_by": "nightly-e2e",
            "idempotency_key": actual, "body": card_body([A])}
    assert diag.nightly_owner([B], state_path=state, board=StateBoard({"t_owner": card})) == ""


@pytest.mark.parametrize("latest", [None, [], [A]])
def test_red_latest_set_is_authoritative_over_stale_legacy_record(tmp_path, latest):
    state = tmp_path / "nightly.json"
    state.write_text(json.dumps({"cards": {
        "red": {"id": "t_owner", "n": 1, "scope": "main", "failing": latest},
        failure_key([B]): {"id": "t_old", "n": 1},
    }}))
    card = {"id": "t_old", "status": "running", "created_by": "nightly-e2e", "body": card_body([B])}
    assert diag.nightly_owner([B], state_path=state, board=StateBoard({"t_old": card})) == ""


def gate_world(monkeypatch, tmp_path):
    monkeypatch.setattr(diag, "DEFAULT_STATE", tmp_path / "nightly-e2e.json")
    (tmp_path / "nightly-e2e.json").write_text("{}")
    monkeypatch.setattr(sync, "git", lambda repo, *args, **kw: "a" * 40 if args[1] == "--verify" else fake_git(repo, *args))
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


def test_main_ci_yaml_e2e_failure_is_a_main_baseline(tmp_path, monkeypatch, capsys):
    github = FakeGitHub(ci_runs=[ci_run(event="workflow_dispatch")],
                        jobs={60: [job(61, "Python tests / e2e", "failure")]}, logs={61: log(A)})
    out = explain(github, tmp_path, monkeypatch, capsys)
    assert f"e2e: main-baseline failure: 1 failing test(s) also fail on main {BASE[:10]} (https://ci.example/run/60)" in out
    assert f"main baseline: {BASE[:10]} (the sync's first parent, on main)" in out


def test_partial_python_slice_set_cannot_prove_candidate_only(tmp_path, monkeypatch, capsys):
    slices = [job(70 + n, f"Python tests / Run tests ({n}/8)", "success" if n != 3 else "cancelled") for n in range(1, 9)]
    github = FakeGitHub(cand_jobs=candidate_jobs(e2e="success", py_slice=3), ci_runs=[ci_run()],
                        jobs={60: slices}, logs={33: log(PY)})
    out = explain(github, tmp_path, monkeypatch, capsys)
    assert "python: baseline comparison unknown for 1 test(s)" in out and "candidate-only" not in out


def test_python_and_e2e_failures_are_attributed_per_kind(tmp_path, monkeypatch, capsys):
    ev = nightly_at(BASE, "failure", log(A))
    slices = [job(70 + n, f"Python tests / Run tests ({n}/8)", "success") for n in range(1, 9)]
    github = FakeGitHub(cand_jobs=candidate_jobs(e2e="failure", py_slice=3), ci_runs=[ci_run()],
                        nightlies=ev["nightlies"], jobs={**ev["jobs"], 60: slices}, logs={**ev["logs"], 33: log(PY)})
    out = explain(github, tmp_path, monkeypatch, capsys)
    assert "python: candidate-only failure observed" in out and "e2e: main-baseline failure" in out


def test_fork_point_falls_back_to_merge_base_when_first_parent_is_not_on_main(tmp_path):
    assert diag.fork_point(tmp_path, "a" * 40, "main", lambda *a, **k: fake_git(*a, first_parent_on_main=False)) == (
        BASE, "merge-base with main")
    assert diag.fork_point(tmp_path, "a" * 40, "main", fake_git) == (BASE, "the sync's first parent, on main")


class StateBoard:
    def __init__(self, cards):
        self.cards = cards

    def card(self, card_id):
        return self.cards.get(card_id)


def test_owner_of_mains_different_set_is_never_labelled_exact(tmp_path, monkeypatch, capsys):
    """Candidate fails {A, B}; main fails {A}; only {A} has a card: it is main's set's owner, not the exact one."""
    github = FakeGitHub(**nightly_at(BASE, "failure", log(A)))
    github.logs[21] = log(A, B)
    state = tmp_path / "nightly-e2e.json"
    key = failure_key([A])
    state.write_text(json.dumps({"cards": {key: {"id": "t_main", "n": 1}}}))
    board = StateBoard({"t_main": {"id": "t_main", "status": "running", "created_by": "nightly-e2e",
                                   "body": card_body([A])}})
    monkeypatch.setattr(diag, "DEFAULT_STATE", state)
    monkeypatch.setattr(diag, "NightlyBoard", lambda *a, **k: board)
    cand = sync.Candidate("a" * 40, "red", "https://ci.example/run/20", 0, run_id=20)
    diag.explain(tmp_path, cand, "main", "example/fork", github.request, github.gh,
                 required=sync.REQUIRED_JOBS, git=fake_git)
    out = capsys.readouterr().out
    assert "no open nightly-e2e card matches this exact e2e failing set" in out
    assert "main's own e2e failing set (a different set) is owned by nightly-e2e card t_main (running)" in out
    assert "owner for this exact failing set" not in out


def test_owner_found_through_the_real_kanban_show_json_shape(tmp_path):
    """The board is a spawned `hermes` fixture printing `kanban show --json`'s field set (no idempotency_key)."""
    key = failure_key([A, B])
    state = tmp_path / "nightly.json"
    state.write_text(json.dumps({"cards": {key: {"id": "t_owner", "n": 1}}}))
    task = {"id": "t_owner", "title": "Nightly e2e RED on main", "body": card_body([A, B]), "assignee": "default",
            "status": "ready", "created_by": "nightly-e2e", "created_at": BASE_TIME, "skills": []}
    fake = tmp_path / "hermes"
    fake.write_text(f"#!{sys.executable}\nimport json, sys\nassert sys.argv[1:] == ['kanban', 'show', 't_owner', "
                    f"'--json'], sys.argv\nprint(json.dumps({{'task': {task!r}, 'comments': []}}))\n")
    fake.chmod(0o755)
    assert diag.nightly_owner([A, B], state_path=state, board=diag.NightlyBoard(str(fake))) == "t_owner (ready)"


def _real_owner_explain(github, tmp_path, monkeypatch, capsys, state_text):
    state = tmp_path / "nightly-e2e.json"
    state.write_text(state_text)
    monkeypatch.setattr(diag, "DEFAULT_STATE", state)
    monkeypatch.setattr(diag, "NightlyBoard", lambda *a, **k: StateBoard({}))
    cand = sync.Candidate("a" * 40, "red", "https://ci.example/run/20", 0, run_id=20)
    diag.explain(tmp_path, cand, "main", "example/fork", github.request, github.gh,
                 required=sync.REQUIRED_JOBS, git=fake_git)
    return capsys.readouterr().out


def test_unavailable_candidate_log_makes_owner_lookup_unknown_not_no_owner(tmp_path, monkeypatch, capsys):
    out = _real_owner_explain(_with(_nightly_green(), logs={21: None}), tmp_path, monkeypatch, capsys, "{}")
    assert "no failing test id parsed" in out
    assert "nightly owner lookup unknown" in out and "no open nightly-e2e card matches" not in out


@pytest.mark.parametrize("state_text", ["{not json", "[]", '{"cards": []}', '{"cards": {"KEY": "t_x"}}'])
def test_corrupt_nightly_state_is_lookup_unavailable_not_no_owner(tmp_path, monkeypatch, capsys, state_text):
    out = _real_owner_explain(FakeGitHub(**nightly_at(BASE, "failure", log(A))), tmp_path, monkeypatch, capsys,
                              state_text.replace("KEY", failure_key([A])))
    assert "main-baseline failure" in out
    assert "nightly owner lookup unavailable" in out and "no open nightly-e2e card matches" not in out


def test_timed_out_log_read_is_an_unavailable_log(tmp_path, monkeypatch, capsys):
    github = FakeGitHub(**nightly_at(BASE, "failure", log(A)))
    plain = github.gh

    def gh(*args, reason=None):
        if "/jobs/21/" in args[1]:
            raise subprocess.TimeoutExpired(["gh", *args], 120)
        return plain(*args, reason=reason)

    github.gh = gh
    out = explain(github, tmp_path, monkeypatch, capsys)
    assert "diagnostics unavailable" not in out and "no failing test id parsed" in out
    assert "main baseline:" in out  # the remaining evidence is still read


WHOLE_FILE_HEADERS = [
    "=== 1 file where the interpreter CRASHED mid-run (native fault — a real bug, not a collection error; "
    "the tests that did run are not counted) ===",
    "=== 2 files that FAILED AT IMPORT/COLLECTION (0 tests ran) ===",
    "=== 1 file where no tests ran (timeout before collection, pytest usage error, etc.) ===",
    "=== ✗ NO TESTS RAN — 0 collected across 3 files ===",
    "(900s exceeded; process tree SIGKILL'd)",
]


@pytest.mark.parametrize("header", WHOLE_FILE_HEADERS)
def test_baseline_whole_file_failure_never_yields_candidate_only(tmp_path, monkeypatch, capsys, header):
    """Main crashed/timed out in A's file (no per-test line) and failed B: A failing on the candidate is unknown."""
    github = FakeGitHub(**nightly_at(BASE, "failure", log(B) + f"\n2026-10-30T16:48:05Z {header}\n"))
    out = explain(github, tmp_path, monkeypatch, capsys)
    assert "candidate-only failure observed" not in out
    assert "e2e: baseline comparison unknown for 1 test(s)" in out


@pytest.mark.parametrize("header", ["=== 1 file with test failures ===", "=== 1 file with pytest ERRORs (fixture "
                                    "setup/teardown; grep '^ERROR ' in the failure output) ==="])
def test_per_test_failure_sections_keep_the_id_set_complete(tmp_path, monkeypatch, capsys, header):
    github = FakeGitHub(**nightly_at(BASE, "failure", log(B) + f"\n2026-10-30T16:48:05Z {header}\n"))
    assert "candidate-only failure observed" in explain(github, tmp_path, monkeypatch, capsys)


CRASH_SECTION = f"\n2026-10-30T16:48:05Z {WHOLE_FILE_HEADERS[0]}\n2026-10-30T16:48:05Z   tests/e2e/core/test_session.py\n"


def test_no_required_failure_points_at_the_run(tmp_path, monkeypatch, capsys):
    out = explain(FakeGitHub(cand_jobs=candidate_jobs(e2e="success")), tmp_path, monkeypatch, capsys)
    assert "no required job failed (missing required job evidence or a non-required failure): see the run" in out


def test_candidate_whole_file_failure_is_not_matched_to_an_owner(tmp_path, monkeypatch, capsys):
    github = FakeGitHub(**nightly_at(BASE, "failure", log(A)))
    github.logs[21] = log(A) + CRASH_SECTION
    out = _real_owner_explain(github, tmp_path, monkeypatch, capsys, "{}")
    assert "main-baseline failure" in out  # the parsed id is still compared
    assert "nightly owner lookup unknown" in out and "no open nightly-e2e card matches" not in out


def test_the_oldest_nightlies_after_the_base_are_read_first(tmp_path, monkeypatch, capsys):
    """Main moved on: twelve later nightlies tested other shas; only the oldest one tested the base."""
    base = nightly_at(BASE, "failure", log(A), run_id=100)
    nightlies, jobs, logs = list(base["nightlies"]), dict(base["jobs"]), dict(base["logs"])
    for n in range(1, 13):
        rid = 100 + 10 * n
        nightlies.append(nightly_run(rid))
        jobs[rid] = [job(rid + 1, "Resolve ref", "success")]
        logs[rid + 1] = f"2026-10-31T00:42:42Z Testing main = {OTHER}\n"
    out = explain(FakeGitHub(nightlies=nightlies, jobs=jobs, logs=logs), tmp_path, monkeypatch, capsys)
    assert "main-baseline failure" in out and "https://ci.example/run/100" in out


def test_spent_log_budget_reads_as_unavailable():
    ticks = iter([0.0, 1.0, 301.0])
    calls = []
    gh = diag.budgeted(lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0, "log", ""), 300,
                       clock=lambda: next(ticks))
    assert gh("api", "x").returncode == 0
    late = gh("api", "y")
    assert late.returncode == 1 and "budget" in late.stderr and calls == [("api", "x")]
