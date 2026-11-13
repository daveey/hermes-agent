"""The macOS state lane's same-PR reuse: key, lookup, gate and save (t_01a1d58663f79436).

On 2026-11-13, 9 of 31 PR runs of ``tests/hermes_state (macOS, mini)`` repeated their branch's
previous passing lane-surface content, on a runner at 0.88 utilisation."""
import subprocess
from pathlib import Path

import pytest

from scripts.ci import macos_state_reuse as reuse
from scripts.ci.classify_changes import COMPARE_FILE_CAP
from scripts.ci.required_results import evaluate_macos_state
from tests.ci.test_macos_state_routing import context, detect, gha, workflow, workflow_steps

LANE = reuse.LANE_WORKFLOW


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    for path, text in {"hermes_state_outcome.py": "a\n", "run_agent.py": "x\n", LANE: "lane\n",
                       "tests/hermes_state/test_x.py": "t\n"}.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def _commit(repo, path, text=None):
    if text is None:
        (repo / path).unlink()
    else:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"edit {path}")


def test_key_ignores_non_surface_pushes(repo):
    key = reuse.reuse_key(repo, ["hermes_state_outcome.py"])
    assert key.startswith(reuse.KEY_PREFIX)
    _commit(repo, "run_agent.py", "y\n")
    # A review fix outside the surface, now also in the PR's diff: same surface content.
    assert reuse.reuse_key(repo, ["hermes_state_outcome.py", "run_agent.py"]) == key


@pytest.mark.parametrize("edit,files", [
    (("hermes_state_outcome.py", "b\n"), ["hermes_state_outcome.py"]),  # new content (PR or main)
    (None, ["hermes_state_outcome.py", "tests/hermes_state/test_x.py"]),  # a routing file joins
    (("tests/hermes_state/test_x.py", None), ["hermes_state_outcome.py", "tests/hermes_state/test_x.py"]),
    ((LANE, "lane v2\n"), ["hermes_state_outcome.py"]),  # the lane workflow changed on main
])
def test_key_changes_with_surface_content(repo, edit, files):
    before = reuse.reuse_key(repo, ["hermes_state_outcome.py"])
    if edit:
        _commit(repo, *edit)
    assert reuse.reuse_key(repo, files) not in ("", before)


def test_deleted_routing_file_is_keyed_as_absent(repo):
    _commit(repo, "tests/hermes_state/test_x.py", None)
    assert reuse.tree_entries(repo, ["tests/hermes_state/test_x.py"]) == ["absent\ttests/hermes_state/test_x.py"]


@pytest.mark.parametrize("files", [[], ["run_agent.py"], [f"docs/{i}.md" for i in range(COMPARE_FILE_CAP)],
                                   [*(f"docs/{i}.md" for i in range(COMPARE_FILE_CAP)), "hermes_state.py"]])
def test_no_key_without_a_bounded_routing_diff(repo, files):
    assert reuse.reuse_key(repo, files) == ""


def test_cli_writes_github_output_and_fails_open(repo, tmp_path, monkeypatch, capsys):
    listing = tmp_path / "files.txt"
    listing.write_text("hermes_state_outcome.py\n")
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert reuse.main(["key", "--files", str(listing), "--repo", str(repo)]) == 0
    assert out.read_text() == f"key={reuse.reuse_key(repo, ['hermes_state_outcome.py'])}\n"
    out.write_text("")
    assert reuse.main(["key", "--files", str(listing), "--repo", str(tmp_path / "missing")]) == 0
    assert out.read_text() == "key=\n"


def _step(job, step_id):
    return next(s for s in workflow("ci.yaml")["jobs"][job]["steps"] if s.get("id") == step_id)


def _gate(ctx, hit):
    """Replay detect's gate-lanes with the reuse lookup's cache-hit."""
    outputs = detect(["hermes_state_outcome.py"], ctx)
    assert outputs["hermes_state"] == "true"
    ctx["steps"]["state-reuse"] = {"outputs": {"cache-hit": hit}}
    step = _step("detect", "gate-lanes")
    return workflow_steps.outputs(step, ctx)["hermes_state"]


@pytest.mark.parametrize("hit,selected", [("true", "false"), ("false", "true"), ("", "true")])
def test_gate_turns_the_lane_off_only_on_a_pull_request_hit(hit, selected):
    assert _gate(context(), hit) == selected


@pytest.mark.parametrize("ctx", [context("workflow_dispatch", "refs/heads/main"), context(release=True),
                                 context("push", "refs/heads/other")])
def test_gate_never_reuses_off_a_pull_request(ctx):
    assert _gate(ctx, "true") == "true"


def _key_step_runs(ctx, selected="true"):
    ctx = {**ctx, "steps": {"classify": {"outputs": {"hermes_state": selected}}}}
    return gha.condition(_step("detect", "state-reuse-key")["if"], ctx)


def test_key_step_only_for_a_selected_same_repo_pull_request():
    assert _key_step_runs(context())
    assert not _key_step_runs(context(fork=True))
    assert not _key_step_runs(context(release=True))
    assert not _key_step_runs(context("workflow_dispatch", "refs/heads/main"))
    assert not _key_step_runs(context(), selected="false")


def test_lookup_and_save_share_key_and_path_and_never_fail_the_gate():
    lookup = _step("detect", "state-reuse")
    record = _step("macos-state-required", "state-pass")
    save = next(s for s in workflow("ci.yaml")["jobs"]["macos-state-required"]["steps"]
                if str(s.get("uses", "")).startswith("actions/cache/save@"))
    assert lookup["uses"].startswith("actions/cache/restore@")
    assert lookup["with"]["lookup-only"] is True
    assert lookup["with"]["path"] == save["with"]["path"]
    assert lookup["with"]["key"] == "${{ steps.state-reuse-key.outputs.key }}"
    assert save["with"]["key"] == "${{ needs.detect.outputs.hermes_state_reuse_key }}"
    assert workflow("ci.yaml")["jobs"]["detect"]["outputs"]["hermes_state_reuse_key"] == \
        "${{ steps.state-reuse-key.outputs.key }}"
    assert all(step.get("continue-on-error") is True for step in (lookup, record, save))
    # A stalled cache endpoint must not hold detect (5 min) or the required check (10 min) until
    # its job timeout: both cache steps give up after 2 min, and a timed-out lookup is a miss.
    assert lookup.get("timeout-minutes") == 2 and save.get("timeout-minutes") == 2
    assert lookup["uses"].split("@")[1] == save["uses"].split("@")[1]


@pytest.mark.parametrize("lane,key,saves", [("success", "k", True), ("skipped", "k", False),
                                             ("failure", "k", False), ("success", "", False)])
def test_only_a_pass_in_this_run_is_recorded(lane, key, saves):
    ctx = context()
    ctx["needs"] = {"hermes-state-macos": {"result": lane}, "detect": {"outputs": {"hermes_state_reuse_key": key}}}
    ctx["__status__"] = {"always": lambda: True, "success": lambda: True}
    assert gha.condition(_step("macos-state-required", "state-pass")["if"], ctx) is saves


def test_a_reused_lane_still_satisfies_the_required_check():
    # Reuse turns the selection off, so the skipped lane passes as unselected.
    assert evaluate_macos_state({"detect": {"result": "success", "outputs": {"hermes_state": "false"}},
                                 "hermes-state-macos": {"result": "skipped"}})["ok"]


def test_lane_runs_no_local_action():
    # classify_changes.HERMES_STATE_WIRING relies on this: another .github/ edit cannot change the lane.
    steps = workflow("hermes-state-macos.yml")["jobs"]["hermes-state-macos"]["steps"]
    assert not [s["uses"] for s in steps if str(s.get("uses", "")).startswith("./")]
    assert Path(workflow("ci.yaml")["jobs"]["hermes-state-macos"]["uses"]).name == Path(LANE).name
