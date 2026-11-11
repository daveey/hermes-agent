"""Duplicate cancellation controls for the separately protected macOS gate."""
import json
import subprocess

import pytest

from tests.ci.test_required_results import _OWN, _api_down, _dup, _fake_fetch, _run_main
from tests.ci.test_macos_state_routing import ROOT, context, gha, workflow


def needs(selected="true", lane="cancelled", detect="success"):
    return {"detect": {"result": detect, "outputs": {"hermes_state": selected}},
            "hermes-state-macos": {"result": lane}}


@pytest.mark.parametrize("selected", ["true", "false", None, "", True, False])
@pytest.mark.parametrize("lane", ["success", "skipped", "failure", "cancelled", None])
@pytest.mark.parametrize("detect", ["success", "cancelled", "failure", None])
def test_only_known_selection_and_cancelled_lane_consults_api(monkeypatch, capsys, selected, lane, detect):
    fetch, calls = _fake_fetch([_dup()])
    code, out = _run_main(monkeypatch, capsys, needs(selected, lane, detect),
                          ["--macos-state", "--allow-superseded"], fetch)
    eligible = detect == "success" and selected in ("true", "false") and lane == "cancelled"
    ordinary = detect == "success" and (selected == "true" and lane == "success" or
                                        selected == "false" and lane in ("success", "skipped"))
    assert bool(calls) is eligible
    assert code == (0 if eligible or ordinary else 1)
    assert ("::error::" in out) is not (eligible or ordinary)


@pytest.mark.parametrize("other", [None, _dup(id=0), _dup(head_sha="different"),
    _dup(workflow_id=8), _dup(event="push"), _dup(head_branch="other"),
    _dup(head_repository={"id": 100}), _dup(status="completed", conclusion="failure"),
    _dup(status="completed", conclusion="cancelled"),
    _dup(run_started_at="2026-11-09T11:45:29Z")])
def test_macos_rejects_non_successor(monkeypatch, capsys, other):
    fetch, _ = _fake_fetch([] if other is None else [other])
    code, out = _run_main(monkeypatch, capsys, needs(), ["--macos-state", "--allow-superseded"], fetch)
    assert code == 1
    assert "::error::" in out


@pytest.mark.parametrize("attempt,prior,expected", [(1, "cancelled", 0), (2, "action_required", 0),
    (2, "cancelled", 1), (2, "success", 1), (3, "action_required", 1), (None, "cancelled", 1)])
def test_macos_rerun_limits(monkeypatch, capsys, attempt, prior, expected):
    fetch, _ = _fake_fetch([_dup()], own={**_OWN, "run_attempt": attempt}, attempt1=prior)
    code, _ = _run_main(monkeypatch, capsys, needs(), ["--macos-state", "--allow-superseded"], fetch)
    assert code == expected


@pytest.mark.parametrize("fetch", [_api_down, lambda path: {}, lambda path: None,
    lambda path: _OWN if path.endswith("runs/1") else []])
def test_macos_lookup_error_fails_closed(monkeypatch, capsys, fetch):
    code, out = _run_main(monkeypatch, capsys, needs(), ["--macos-state", "--allow-superseded"], fetch)
    assert code == 1
    assert "keeping the failure" in out
    assert "::error::" in out


def test_macos_without_opt_in_never_fetches(monkeypatch, capsys):
    fetch, calls = _fake_fetch([_dup()])
    code, _ = _run_main(monkeypatch, capsys, needs(), ["--macos-state"], fetch)
    assert code == 1
    assert not calls


def test_macos_release_rejects_delegation(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, capsys, needs(),
                  ["--macos-state", "--release", "--allow-superseded"], _api_down)
    assert exc.value.code == 2


@pytest.mark.parametrize("event,release,expected", [
    ("pull_request", False, ["--macos-state", "--allow-superseded"]),
    ("pull_request", True, ["--macos-state", "--release"]),
    ("push", False, ["--macos-state"]),
    ("workflow_dispatch", False, ["--macos-state"]),
    ("workflow_call", True, ["--macos-state", "--release"]),
])
def test_workflow_passes_delegation_only_for_nonrelease_pr(event, release, expected):
    ctx = context(event=event, release=release)
    ctx["github"]["token"] = "fixture-token"
    ctx["needs"] = needs()
    job = workflow("ci.yaml")["jobs"]["macos-state-required"]
    step = next(step for step in job["steps"] if "run" in step)
    env = {key: gha.to_string(gha.render(value, ctx)) for key, value in step["env"].items()}
    # Execute the workflow's shell argument construction, recording its CLI boundary.
    shim = 'python3() { cat >/dev/null; printf "%s\\n" "$@"; }; export -f python3\n'
    proc = subprocess.run(["bash", "-eo", "pipefail", "-c", shim + gha.render(step["run"], ctx)],
                          cwd=ROOT, env=env, text=True, encoding="utf-8", capture_output=True,
                          timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["scripts/ci/required_results.py", *expected]
    assert json.loads(env["NEEDS"]) == needs()
    assert env["GH_TOKEN"] == "fixture-token"
