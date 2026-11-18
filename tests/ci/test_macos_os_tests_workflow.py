"""The scheduled macOS OS-marked lane: shape of macos-os-tests.yml and its mini-queue gate.

Decision t_01a1d5b9382b24ad (option B): platforms("macos"/"posix") tests run on the
Mac mini on a schedule only, never on a pull request, and yield to required mini jobs.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
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
    assert '-m "not integration and not macos_login_session"' in command


@pytest.mark.parametrize("domain_status", [0, 125])
def test_login_session_job_is_gated_and_requires_a_real_gui_domain(domain_status):
    job = _workflow()["jobs"]["macos-login-session-tests"]
    assert job["runs-on"] == ["self-hosted", "macOS", "hermes-mac-session"]
    assert job["needs"] == "gate"
    assert "needs.gate.outputs.run == 'true'" in job["if"]
    assert f"github.repository == '{REPO}'" in job["if"]
    guard = job["steps"][0]["run"]
    assert 'launchctl print "gui/$(id -u)"' in guard
    assert "set -euo pipefail" in guard
    assert "|| true" not in guard
    # Execute the workflow's shell contract with a controlled launchctl boundary.
    probe = subprocess.run(
        ["bash", "-c", f'launchctl() {{ printf "%s\\n" "$*" >&2; return {domain_status}; }}\n{guard}'],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert probe.returncode == domain_status
    assert "print gui/" in probe.stderr
    command = job["steps"][-1]["run"]
    assert '-m "macos_login_session"' in command
    assert 'test -s "$LIST"' in command
    assert "taskpolicy -c utility scripts/run_tests.sh" in command
    assert any(step.get("uses") == "./.github/actions/setup-pm" for step in job["steps"])


@pytest.mark.parametrize("marker", ["macos", "windows"])
def test_reusable_lane_preserves_windows_and_widens_only_macos(marker):
    workflow = hermes_yaml.safe_load((ROOT / ".github/workflows/tests-os.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["os-tests"]
    assert any(row["marker"] == marker for row in job["strategy"]["matrix"]["include"])
    step = next(step for step in job["steps"] if step.get("name") == "Run ${{ matrix.marker }} tests")
    command = step["run"]
    assert '-m "$MARKERS"' in command
    # Execute the actual selection branch, not a test-side copy of its conditions.
    selection = command.split('MARKERS=', 1)
    assert len(selection) == 2
    shell = 'MARKERS=' + selection[1].split('scripts/run_tests.sh', 1)[0]
    shell = shell.replace("${{ matrix.marker }}", marker)
    result = subprocess.run(
        ["bash", "-eu", "-c", shell + '\nprintf "%s" "$MARKERS"'],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    expected = "not integration and not macos_login_session" if marker == "macos" else "platforms and not integration"
    assert result.stdout == expected


def test_unmarked_darwin_regression_is_collected_in_widened_lane():
    files, expression = _lane("macos-os-tests")
    path = "tests/hermes_cli/test_loop_accounting.py"
    node = path + "::test_without_procfs_snapshot_folds_exited_threads"
    assert path in files
    assert node in _collected([path], expression)
    assert node not in _collected([path], "platforms and not integration")


def test_session_runner_label_is_declared_for_actionlint():
    config = hermes_yaml.safe_load((ROOT / ".github/actionlint.yaml").read_text(encoding="utf-8"))
    assert "hermes-mac-session" in config["self-hosted-runner"]["labels"]


# The five login-session cases (three functions, one parametrized three ways), as pytest names them.
_LOGIN_SESSION_CASES = {
    "tests/hermes_cli/test_gateway_launchd_runtime.py::"
    "test_gateway_job_is_a_launchd_osascript_child_that_reads_as_unsupervised",
    *(
        "tests/hermes_cli/test_gateway_snapshot_restore_live.py::"
        f"test_snapshot_restore_rejoins_the_restored_authority[{case}]"
        for case in ("standalone-resume-restored", "satellite-create-missing", "legacy-reimport-leased")
    ),
    "tests/hermes_cli/test_update_posix_pause.py::"
    "test_a_launchd_job_is_paused_through_launchd_and_restarted_through_it",
}
_LOGIN_SESSION_FILES = sorted({case.split("::")[0] for case in _LOGIN_SESSION_CASES})


def _lane(job: str) -> tuple[set[str], str]:
    """The files a lane imports and its ``-m`` expression, both taken from how the workflow job runs."""
    command = _workflow()["jobs"][job]["steps"][-1]["run"]
    selector = re.search(r"scripts/ci/list_os_marked_tests\.py (\w+)", command)
    expression = re.search(r'-m "([^"]+)"', command)
    assert selector and expression, command
    listed = subprocess.run(
        [sys.executable, "scripts/ci/list_os_marked_tests.py", selector.group(1)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120, check=True,
    )
    return set(listed.stdout.split()), expression.group(1)


def _collected(files: list[str], expression: str) -> set[str]:
    """Node IDs pytest itself selects from *files* under *expression*, from each item's effective markers.

    ``--strict-markers`` turns an unregistered ``macos_login_session`` into a collection error.
    """
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "--strict-markers",
            "--rootdir", str(ROOT), "-c", str(ROOT / "pyproject.toml"), "-m", expression, *files,
        ],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=300, check=False,
    )
    # 5 = every item deselected; any other non-zero exit is a collection error.
    assert result.returncode in (0, 5), result.stdout[-3000:] + result.stderr[-3000:]
    return {line.strip() for line in result.stdout.splitlines() if "::" in line}


def test_login_session_cases_reach_the_session_lane_and_never_the_room_lane():
    session_files, session_expression = _lane("macos-login-session-tests")
    room_files, room_expression = _lane("macos-os-tests")
    # Discovery prerequisite: a file the lane's real selector omits is never imported, so its
    # marked cases vanish before -m sees them (e.g. a test losing the platforms() gate it keys on).
    assert set(_LOGIN_SESSION_FILES) <= session_files, set(_LOGIN_SESSION_FILES) - session_files
    assert _collected(_LOGIN_SESSION_FILES, session_expression) == _LOGIN_SESSION_CASES
    in_room = _collected(sorted(room_files & set(_LOGIN_SESSION_FILES)), room_expression)
    assert in_room, "the room lane no longer selects the other platforms() cases of these files"
    assert not in_room & _LOGIN_SESSION_CASES


def test_native_lane_provisions_real_tools_and_messaging_before_tests():
    import json

    steps = _workflow()["jobs"]["macos-os-tests"]["steps"]
    setup_index = next(i for i, step in enumerate(steps)
                       if step.get("uses") == "./.github/actions/setup-pm")
    options = steps[setup_index]["with"]
    assert options["toolchain"] == "all"  # includes real uv, Node and paired npm
    assert options["test-environment"] == "true"
    assert {"all", "messaging"} <= set(json.loads(options["extras"]))
    assert setup_index < next(i for i, step in enumerate(steps)
                              if "scripts/run_tests.sh" in step.get("run", ""))


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
