"""The scheduled macOS OS-marked lane: shape of macos-os-tests.yml and its mini-queue gate.

Decision t_01a1d5b9382b24ad (option B): platforms("macos"/"posix") tests run on the
Mac mini on a schedule only, never on a pull request, and yield to required mini jobs.
"""
from __future__ import annotations

import importlib.util
import subprocess
import types
from pathlib import Path

import hermes_yaml
import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/macos-os-tests.yml"
GATE = ROOT / "scripts/ci/macos_os_tests_gate.py"
REPO = "softmax-universe/daveey-hermes"


def _gate():
    spec = importlib.util.spec_from_file_location("macos_os_tests_gate", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workflow():
    return hermes_yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_lane_is_scheduled_and_never_a_pull_request_lane():
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch", "push"}
    assert triggers["schedule"] == [{"cron": "37 0,12 * * *"}]
    # Probe branches only: ci-probe/** would also start the hermes_state lane on the mini.
    assert triggers["push"] == {"branches": ["macos-os-probe/**"]}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    ci = (ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8")
    assert "uses: ./.github/workflows/macos-os-tests.yml" not in ci


def test_test_job_waits_for_the_gate_and_runs_the_marked_selection_on_the_mini():
    jobs = _workflow()["jobs"]
    gate, job = jobs["gate"], jobs["macos-os-tests"]
    assert gate["permissions"] == {"actions": "read", "contents": "read"}
    assert "scripts/ci/macos_os_tests_gate.py" in gate["steps"][-1]["run"]
    assert gate["outputs"]["run"] == "${{ steps.gate.outputs.run }}"
    assert job["needs"] == "gate"
    assert "needs.gate.outputs.run == 'true'" in job["if"]
    assert f"github.repository == '{REPO}'" in job["if"]
    assert job["runs-on"] == ["self-hosted", "macOS", "hermes-state"]
    command = job["steps"][-1]["run"]
    assert "sys.platform == 'darwin'" in command
    assert "HERMES_HOME" in command and "mktemp -d" in command
    assert "scripts/ci/list_os_marked_tests.py macos" in command
    assert 'test -s "$LIST"' in command
    assert "taskpolicy -c utility scripts/run_tests.sh --files-from \"$LIST\" -j 2" in command
    assert '-m "platforms and not integration"' in command


def _api(runs: dict[str, list[dict]], jobs: dict[int, list[dict]], calls: list[str] | None = None):
    def api(path: str):
        if calls is not None:
            calls.append(path)
        if "/jobs" in path:
            run_id = int(path.split("/runs/")[1].split("/")[0])
            return {"jobs": jobs.get(run_id, [])}
        state = path.split("status=")[1].split("&")[0]
        return {"total_count": len(runs.get(state, [])), "workflow_runs": runs.get(state, [])}
    return api


def _job(status: str, labels=("self-hosted", "macOS", "hermes-state"), name="hermes_state (macOS) / x"):
    return {"status": status, "labels": list(labels), "name": name}


@pytest.mark.parametrize("status", ["queued", "waiting", "pending", "requested"])
def test_schedule_skips_while_a_mini_job_waits(status):
    api = _api({"in_progress": [{"id": 1}]}, {1: [_job("completed"), _job(status)]})
    run, reason = _gate().decide("schedule", api, REPO, own_run_id=99)
    assert run is False
    assert "1 job(s) already waiting" in reason


def test_schedule_runs_when_mini_jobs_are_only_running_or_on_other_labels():
    jobs = {
        1: [_job("in_progress")],
        2: [_job("queued", labels=("blacksmith-4vcpu-ubuntu-2404",))],
    }
    api = _api({"in_progress": [{"id": 1}], "queued": [{"id": 2}]}, jobs)
    assert _gate().decide("schedule", api, REPO, own_run_id=99) == (True, "no job waiting for the mini")


def test_own_run_is_excluded_and_each_run_is_read_once():
    calls: list[str] = []
    jobs = {99: [_job("queued", name="macOS-only tests (mini)")], 1: []}
    api = _api({"queued": [{"id": 99}, {"id": 1}], "in_progress": [{"id": 1}]}, jobs, calls)
    run, _ = _gate().decide("schedule", api, REPO, own_run_id=99)
    assert run is True
    assert sum("/runs/1/jobs" in c for c in calls) == 1
    assert not any("/runs/99/jobs" in c for c in calls)


def _paged_api(pages: dict[str, list[list[dict]]], jobs: dict[int, list[dict]], calls: list[str]):
    """Like _api, but each state's runs are split into pages served by &page=N (1 when absent)."""
    def api(path: str):
        calls.append(path)
        if "/jobs" in path:
            run_id = int(path.split("/runs/")[1].split("/")[0])
            return {"jobs": jobs.get(run_id, [])}
        state = path.split("status=")[1].split("&")[0]
        page = int(path.split("page=")[-1].split("&")[0]) if "&page=" in path else 1
        listing = pages.get(state, [])
        return {"total_count": sum(map(len, listing)),
                "workflow_runs": listing[page - 1] if page <= len(listing) else []}
    return api


def test_a_mini_job_waiting_only_in_a_later_page_of_runs_still_skips_the_slot():
    # 100 in-progress runs fill page 1 with nothing waiting; the waiting mini job sits on page 2.
    calls: list[str] = []
    page1 = [{"id": i} for i in range(1, 101)]
    api = _paged_api({"in_progress": [page1, [{"id": 500}]]}, {500: [_job("queued")]}, calls)
    run, reason = _gate().decide("schedule", api, REPO, own_run_id=99)
    assert run is False
    assert "1 job(s) already waiting" in reason
    assert any("status=in_progress" in c and "&page=2" in c for c in calls)
    # A final short page reaches total_count: no page 3 read.
    assert not any("status=in_progress" in c and "&page=3" in c for c in calls)


@pytest.mark.parametrize("final_job", [None, "in_progress", "queued"])
def test_final_supported_page_is_read_before_deciding_saturation(final_job):
    gate = _gate()
    calls: list[str] = []
    pages = [
        [{"id": p * gate.PAGE_SIZE + i + 1} for i in range(gate.PAGE_SIZE)]
        for p in range(gate.MAX_RUN_PAGES - 1)
    ]
    final_id = gate.MAX_RUN_PAGES * gate.PAGE_SIZE + 1
    pages.append([] if final_job is None else [{"id": final_id}])
    jobs = {} if final_job is None else {final_id: [_job(final_job)]}
    api = _paged_api({"queued": pages}, jobs, calls)

    run, reason = gate.decide("schedule", api, REPO, own_run_id=0)

    prefix = f"repos/{REPO}/actions/runs?status=queued&per_page={gate.PAGE_SIZE}&page="
    assert [c for c in calls if c.startswith(prefix)] == [
        f"{prefix}{page}" for page in range(1, gate.MAX_RUN_PAGES + 1)
    ] * 2
    if final_job == "queued":
        assert run is False
        assert "1 job(s) already waiting for the mini" in reason
    else:
        assert (run, reason) == (True, "no job waiting for the mini")
    if final_job is not None:
        assert calls.count(
            f"repos/{REPO}/actions/runs/{final_id}/jobs?filter=latest&per_page=100"
        ) == 1


@pytest.mark.parametrize("at_cap", [False, True], ids=["below-cap", "at-cap"])
def test_short_page_stops_when_offset_equals_total_count(at_cap):
    gate = _gate()
    gate.PAGE_SIZE = 2
    final_page = gate.MAX_RUN_PAGES if at_cap else 2
    calls: list[str] = []
    pages = [[{"id": 2 * p + 1}, {"id": 2 * p + 2}] for p in range(final_page - 1)]
    pages.append([{"id": 2 * final_page - 1}])
    listing = _paged_api({"queued": pages}, {}, calls)

    def api(path):
        response = listing(path)
        if "status=queued" in path:
            # Filtered pages can contain fewer rows than the reported offset total.
            response["total_count"] = final_page * gate.PAGE_SIZE
        return response

    assert gate.decide("schedule", api, REPO, own_run_id=0) == (
        True, "no job waiting for the mini"
    )
    prefix = f"repos/{REPO}/actions/runs?status=queued&per_page={gate.PAGE_SIZE}&page="
    assert [c for c in calls if c.startswith(prefix)] == [
        f"{prefix}{page}" for page in range(1, final_page + 1)
    ] * 2


@pytest.mark.parametrize("reported_total", [None, 45])
def test_short_nonterminal_page_does_not_hide_a_waiting_mini_job(reported_total):
    gate = _gate()
    gate.PAGE_SIZE = 5
    calls: list[str] = []
    # Captured GitHub geometry: queued page 1 had four rows, total_count=45,
    # rel=next page 2; page 2 still had five rows. Eligibility is controlled.
    pages = {"queued": [[{"id": i} for i in range(1, 5)], [{"id": 6}]]}
    listing = _paged_api(pages, {6: [_job("queued")]}, calls)

    def api(path):
        response = listing(path)
        if "workflow_runs" in response:
            response.pop("total_count", None)
            if reported_total is not None:
                response["total_count"] = reported_total if "status=queued" in path else 0
        return response

    assert gate.decide("schedule", api, REPO, own_run_id=99)[0] is False
    assert any("/runs/6/jobs" in c for c in calls)


def test_confirmation_recovers_waiting_run_shifted_behind_first_pass_offset():
    gate = _gate()
    gate.PAGE_SIZE = 2
    calls: list[str] = []
    pass_number = 0

    def api(path):
        nonlocal pass_number
        calls.append(path)
        if "/jobs" in path:
            return {"jobs": [_job("queued")] if "/runs/3/jobs" in path else []}
        if "status=queued" not in path:
            return {"total_count": 0, "workflow_runs": []}
        page = int(path.split("&page=")[1])
        if page == 1:
            pass_number += 1
        # First page sees [1, 2, 3]; run 1 completes before page 2, so
        # run 3 moves to offset 1, behind the cursor. It remains waiting.
        population = [1, 2, 3] if pass_number == 1 and page == 1 else [2, 3]
        return {"total_count": len(population),
                "workflow_runs": [{"id": i} for i in population[(page - 1) * 2:page * 2]]}

    assert gate.decide("schedule", api, REPO, own_run_id=99)[0] is False
    assert pass_number == 2
    assert sum("/runs/3/jobs" in c for c in calls) == 1


def test_a_state_with_more_full_pages_than_the_cap_is_treated_as_busy():
    gate = _gate()
    calls: list[str] = []
    full = [[{"id": p * 1000 + i} for i in range(gate.PAGE_SIZE)] for p in range(gate.MAX_RUN_PAGES)]
    api = _paged_api({"queued": full}, {}, calls)
    run, reason = gate.decide("schedule", api, REPO, own_run_id=99)
    assert run is False
    assert "queued runs open; skipping this slot" in reason
    assert not any("/jobs" in c for c in calls)


@pytest.mark.parametrize("event", ["workflow_dispatch", "push"])
def test_manual_and_probe_runs_are_never_gated(event):
    def api(path):
        raise AssertionError("not read")
    assert _gate().decide(event, api, REPO, own_run_id=1)[0] is True


@pytest.mark.parametrize("error", [subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502"),
                                   subprocess.TimeoutExpired(["gh"], 60), ValueError("bad JSON")])
def test_unreadable_queue_fails_open_to_testing(error):
    def api(path):
        raise error
    run, reason = _gate().decide("schedule", api, REPO, own_run_id=1)
    assert run is True
    assert "running anyway" in reason


def test_a_hung_gh_read_is_bounded_and_fails_open_to_testing(monkeypatch):
    # The real gh_api must bound each `gh api` call: without a timeout a hung read holds the
    # gate job until its 10-minute cap, the gate fails and the scheduled test job is skipped.
    gate = _gate()
    timeouts: list[object] = []

    def run(argv, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)

    # Patch the gate module's own subprocess name, not the global module.
    fake = types.SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError,
                                 TimeoutExpired=subprocess.TimeoutExpired,
                                 CalledProcessError=subprocess.CalledProcessError)
    monkeypatch.setattr(gate, "subprocess", fake)
    run_it, reason = gate.decide("schedule", gate.gh_api, REPO, own_run_id=1)
    assert run_it is True
    assert "running anyway" in reason
    assert timeouts, "gh_api never called subprocess.run"
    assert all(isinstance(t, (int, float)) and 0 < t <= 120 for t in timeouts), timeouts


def test_main_writes_the_step_output(tmp_path, monkeypatch):
    gate = _gate()
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_RUN_ID", "5")
    monkeypatch.setattr(gate, "gh_api", _api({"queued": [{"id": 6}]}, {6: [_job("queued")]}))
    assert gate.main() == 0
    assert out.read_text() == "run=false\n"
