"""scripts/ci/affected_tests.py: which test files a pull request runs (tests.yml `plan` job)."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "affected_tests.py"
_spec = importlib.util.spec_from_file_location("affected_tests_mod", _PATH)
at = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(at)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    files = {
        "pkg/__init__.py": "",
        "pkg/core.py": "VALUE = 1\n",
        "pkg/mid.py": "from pkg import core\n",
        "pkg/top.py": "def f():\n    import pkg.mid\n",  # function-level import still counts
        "pkg/other.py": "X = 2\n",
        "scripts/tool.sh": "echo hi\n",
        "tests/__init__.py": "",
        "tests/pkg/test_top.py": "from pkg import top\n",
        "tests/pkg/test_other.py": "import pkg.other\n",
        "tests/pkg/test_patch.py": "def test(monkeypatch):\n    monkeypatch.setattr('pkg.core.VALUE', 2)\n",
        "tests/pkg/test_script.py": "SCRIPT = 'scripts/tool.sh'\n",
        "tests/pkg/test_scan.py": "from pathlib import Path\nFILES = list(Path('.').rglob('*.py'))\n",
        "tests/ci/test_wf.py": "X = 1\n",
        "tests/e2e/test_slow.py": "import pkg.core\n",
    }
    files.update({f"tests/filler/test_f{i}.py": "X = 1\n" for i in range(20)})
    _write(tmp_path, files)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


SMOKE = {"tests/pkg/test_scan.py", "tests/ci/test_wf.py"}


def _files(repo, changed):
    result = at.plan(repo, changed)
    assert result["mode"] == "affected", result
    return set(result["files"])


def test_changed_test_file_runs(repo):
    assert _files(repo, ["tests/pkg/test_other.py"]) == {"tests/pkg/test_other.py"} | SMOKE


@pytest.mark.parametrize("changed", [
    "pkg/other.py\tpkg/core.py", "pkg/core.py\tpkg/other.py",
])
def test_renamed_module_selects_both_sides(repo, changed):
    assert _files(repo, [changed]) == _files(repo, ["pkg/other.py", "pkg/core.py"])


@pytest.mark.parametrize("changed", [
    "docs/old-fixtures.md\ttests/conftest.py", "tests/conftest.py\tdocs/old-fixtures.md",
])
def test_renamed_setup_emits_full_cli_plan(repo, tmp_path, changed):
    gh_out = tmp_path / "rename-gh.txt"
    assert at.main(["--repo", str(repo), "--changed", changed, "--github-output", str(gh_out)]) == 0
    outputs = dict(line.split("=", 1) for line in gh_out.read_text().splitlines())
    assert outputs["mode"] == "full" and "tests/conftest.py" in outputs["reason"]


def test_transitive_and_function_level_importers_run(repo):
    # core <- mid <- top (inside a function) <- test_top
    assert {"tests/pkg/test_top.py", "tests/pkg/test_patch.py"} <= _files(repo, ["pkg/core.py"])
    assert "tests/pkg/test_other.py" not in _files(repo, ["pkg/core.py"])


@pytest.fixture
def gate_repo(repo):
    # The gate is copied/spawned, not imported. Consumers only import the fake.
    _write(repo, {
        "tests/hermes_cli/upstream_gate_fake.py": "class FakeFork: pass\n",
        "tests/hermes_cli/gate_adapter.py": "from .upstream_gate_fake import FakeFork\n",
        "tests/hermes_cli/test_direct.py": "from tests.hermes_cli.upstream_gate_fake import FakeFork\n",
        "tests/hermes_cli/test_indirect.py": "from tests.hermes_cli.gate_adapter import FakeFork\n",
        "tests/e2e/test_gate.py": "from tests.hermes_cli.upstream_gate_fake import FakeFork\n",
    })
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    return repo


@pytest.mark.parametrize("changed", [
    "scripts/ci/upstream_sync.py",
    "scripts/ci/upstream_sync_identity.py",
    "scripts/ci/upstream_sync_future.py",  # also covers deleted/untracked gate siblings
    "scripts/ci/upstream_sync_old.py\tscripts/ci/renamed_gate.py",
    "scripts/ci/renamed_gate.py\tscripts/ci/upstream_sync_new.py",
])
def test_copied_gate_selects_direct_and_transitive_consumers(gate_repo, tmp_path, changed):
    files_out = tmp_path / "gate-files.txt"
    assert at.main(["--repo", str(gate_repo), "--changed", changed,
                    "--files-out", str(files_out)]) == 0
    assert set(files_out.read_text().splitlines()) == SMOKE | {
        "tests/hermes_cli/test_direct.py", "tests/hermes_cli/test_indirect.py",
    }


@pytest.mark.parametrize("changed", [
    "scripts/ci/other.py", "elsewhere/upstream_sync.py",
    "scripts/ci/upstream_sync.md", "scripts/ci/upstream_sync/nested.py",
])
def test_unrelated_changes_do_not_select_gate_consumers(gate_repo, changed):
    assert _files(gate_repo, [changed]) == SMOKE


def test_real_updater_consumers_are_selected_for_gate_change():
    selected = _files(_PATH.parents[2], ["scripts/ci/upstream_sync_identity.py"])
    assert {
        "tests/hermes_cli/test_update_fork_merge_upstream.py",
        "tests/hermes_cli/test_update_fork_sync_identity.py",
    } <= selected


def test_string_patch_target_runs(repo):
    assert "tests/pkg/test_patch.py" in _files(repo, ["pkg/core.py"])
    assert "tests/pkg/test_patch.py" not in _files(repo, ["pkg/other.py"])


def test_non_python_file_named_by_a_test_runs(repo):
    assert "tests/pkg/test_script.py" in _files(repo, ["scripts/tool.sh"])


def test_unreferenced_change_still_runs_the_smoke_set(repo):
    assert _files(repo, ["docs/guide.md"]) == SMOKE


def test_files_outside_the_full_suite_are_never_selected(repo):
    # tests/e2e is skipped by run_tests_parallel.py discovery: a PR must not run it either.
    assert "tests/e2e/test_slow.py" not in _files(repo, ["pkg/core.py"])
    assert "tests/e2e/test_slow.py" not in _files(repo, ["tests/e2e/test_slow.py"])


@pytest.mark.parametrize("wide", ["tests/conftest.py", "pkg/conftest.py", "uv.lock", "pyproject.toml",
                                  "scripts/run_tests_parallel.py", "scripts/ci/affected_tests.py",
                                  ".github/actions/setup-pm/action.yml", "tests/fakes.py"])
def test_setup_wide_changes_run_the_full_suite(repo, wide):
    result = at.plan(repo, ["pkg/other.py", wide])
    assert result["mode"] == "full" and wide in result["reason"] and result["slices"] == at.MAX_SLICES


@pytest.mark.parametrize("wide", [
    "pm/lock.json", "pm/environments.py", "pm/testenv.py",
    "scripts/_activation.sh", "scripts/ci/setup_toolchain.py",
])
def test_shared_toolchain_changes_run_the_full_suite(repo, wide):
    result = at.plan(repo, [wide])
    assert result["mode"] == "full" and wide in result["reason"]
    assert result["files"] == [] and result["slices"] == at.MAX_SLICES


def test_all_activation_inputs_require_full_suite(repo):
    from pm.environments import ACTIVATION_INPUTS

    for path in ACTIVATION_INPUTS:
        assert at.plan(repo, [path])["mode"] == "full", path


def test_lock_only_interpreter_bump_emits_full_cli_plan(repo, tmp_path):
    lock = repo / "pm" / "lock.json"
    lock.parent.mkdir()
    lock.write_text(json.dumps({"python": {"version": "3.14.7"}}))
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    lock.write_text(json.dumps({"python": {"version": "3.14.9"}}))
    subprocess.run([*git, "commit", "-qam", "bump interpreter"], check=True)
    assert at.changed_files(repo, "HEAD^", "HEAD") == ["pm/lock.json"]
    gh_out, files_out = tmp_path / "lock-gh.txt", tmp_path / "lock-files.txt"
    assert at.main(["--repo", str(repo), "--github-output", str(gh_out),
                    "--files-out", str(files_out)]) == 0
    outputs = dict(line.split("=", 1) for line in gh_out.read_text().splitlines())
    assert outputs["mode"] == "full" and "pm/lock.json" in outputs["reason"]
    assert json.loads(outputs["slices"]) == list(range(1, at.MAX_SLICES + 1))
    assert files_out.read_text() == ""


def test_unrelated_pm_data_keeps_narrow_selection(repo):
    assert _files(repo, ["pm/artifact-mirror.json"]) == SMOKE


def test_large_selection_runs_the_full_suite(repo, monkeypatch):
    monkeypatch.setattr(at, "FULL_FRACTION", 0.1)
    assert at.plan(repo, ["pkg/core.py"])["mode"] == "full"


def test_explicit_full_and_empty_diff(repo):
    assert at.plan(repo, [], "push to main")["mode"] == "full"
    assert at.plan(repo, [])["mode"] == "none"


def test_slices_follow_measured_durations(repo):
    (repo / "test_durations.json").write_text(json.dumps({"tests/pkg/test_top.py": 3 * at.SLICE_SECONDS}))
    assert at.plan(repo, ["pkg/core.py"])["slices"] == min(4, at.MAX_SLICES)
    assert at.plan(repo, ["pkg/other.py"])["slices"] == 1
    assert at.slices_for(100 * at.SLICE_SECONDS) == at.MAX_SLICES


def test_cli_writes_files_and_github_outputs(repo, tmp_path):
    files_out, gh_out = tmp_path / "files.txt", tmp_path / "gh.txt"
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "a"],
                   check=True)
    (repo / "pkg" / "other.py").write_text("X = 3\n")
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "b"],
                   check=True)
    assert at.main(["--repo", str(repo), "--files-out", str(files_out), "--github-output", str(gh_out)]) == 0
    assert set(files_out.read_text().split()) == {"tests/pkg/test_other.py"} | SMOKE
    outputs = dict(line.split("=", 1) for line in gh_out.read_text().splitlines())
    assert outputs["mode"] == "affected" and outputs["slices"] == "[1]" and outputs["count"] == "3"


def test_cli_without_a_diff_runs_the_full_suite(repo, tmp_path):
    gh_out = tmp_path / "gh.txt"
    assert at.main(["--repo", str(repo), "--base", "nope", "--github-output", str(gh_out)]) == 0
    outputs = dict(line.split("=", 1) for line in gh_out.read_text().splitlines())
    assert outputs["mode"] == "full" and "git diff" in outputs["reason"]
    assert json.loads(outputs["slices"]) == list(range(1, at.MAX_SLICES + 1))
