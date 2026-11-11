"""scripts/check runner, git hooks, CLI output modes and regex-rule scope, on real git repos.

Fixture repos carry a copy of this checkout's engine (scripts/check, scripts/code_health, the
guard scripts, pyproject's ruff pin) so the runner and the hooks execute exactly as they would
in a clone, against tiny trees that keep every check fast.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from scripts import check_config_yaml_readers, check_config_yaml_writers
from scripts.code_health import cli, gitio
from scripts.code_health.config import ENFORCEMENT

REPO = Path(__file__).resolve().parents[2]
_GUARD_SCRIPTS = (
    "scripts/check-windows-footguns.py", "scripts/check_bash_shebangs.py",
    "scripts/check_no_tmp_literals.py", "scripts/check_detached_spawns.py", "scripts/install_root_lint.py",
    "scripts/check_config_yaml_writers.py", "scripts/check_config_yaml_readers.py",
    "scripts/check_profile_create_entry.py", "scripts/ci/check_os_marker_fakes.py", "scripts/check-case-collisions.py",
    "scripts/ci/check_lazy_deps_imports.py", "scripts/ci/check_profile_archive_boundary.py",
    "scripts/ci/check_agents_md_size.py", "scripts/ci/check_blob_size.py",
)
_SUPPORT = ("scripts/ci/profile_scope_patterns.json", "scripts/install_root_lint_baseline.json", "agent/subdirectory_hints.py", *_GUARD_SCRIPTS)
# The config guards fail when a tree they scan is missing, so the fixture carries an empty stand-in.
_SCANNED_TREES = sorted({*check_config_yaml_readers.DEFAULT_TREES, *check_config_yaml_writers.DEFAULT_TREES})
_LEGACY = "def legacy(x):\n" + "".join(f"    if x == {i}:\n        return {i}\n" for i in range(21))
_GROWN = _LEGACY + "    if x == 99:\n        return 99\n"
_ENV_COPY = "import os\n\n\ndef child_env():\n    env = os.environ.copy()\n    return env\n"
# A violation of a rule that always blocks (PS-P05 is warning-only until replayed).
_SWALLOW = "def swallow():\n    try:\n        pass\n    except Exception:\n        pass\n"
_SWITCH = "scripts/code_health/config.py"


def _env() -> dict[str, str]:
    """The caller's env minus anything that would point git at another repo or config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


def _sh(cwd: Path, *argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), cwd=cwd, env=env or _env(), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                          timeout=600, check=False)


def _git(repo: Path, *args: str) -> str:
    proc = _sh(repo, "git", *args)
    assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
    return proc.stdout.strip()


def _write(repo: Path, files: Mapping[str, str | None]) -> None:
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")


def _commit(repo: Path, files: Mapping[str, str | None]) -> str:
    _write(repo, files)
    _git(repo, "add", "--all", "--", *files)
    _git(repo, "commit", "-q", "-m", "step")
    return _git(repo, "rev-parse", "HEAD")


def _init(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / ".gitignore").write_text(".venv\n", encoding="utf-8")
    shutil.copy(REPO / "pyproject.toml", path / "pyproject.toml")
    return path


def _add_engine(repo: Path, checker: bool = True) -> list[str]:
    """What the checker's jobs read (guard scripts, policy, a tests/ dir) and, with ``checker``,
    scripts/check + scripts/code_health themselves; returns the top-level paths to stage."""
    for rel in _SUPPORT:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, repo / rel)
    _write(repo, {"tests/README.md": "os-marker-fakes scans tests/\n"})

    # The prompt-surface guard renders the whole product through its own dependencies, which this
    # toy repo has none of; a stub that passes keeps the guard registered and the hooks' flow real.
    # (The real guard is covered by tests/ci/test_prompt_surface.py.)
    _write(repo, {"scripts/ci/prompt_surface.py": "import sys\n\nsys.exit(0)\n"})
    _write(repo, {tree if tree.endswith(".py") else f"{tree}/__init__.py": "" for tree in _SCANNED_TREES
                  if not (repo / tree).exists()})
    if checker:
        shutil.copy2(REPO / "scripts/check", repo / "scripts/check")
        shutil.copy2(REPO / "scripts/ci/upstream_sync.py", repo / "scripts/ci/upstream_sync.py")
        shutil.copytree(REPO / "scripts/code_health", repo / "scripts/code_health",
                        ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
    return sorted({"agent", "scripts", "tests", *_SCANNED_TREES})


def _engine_repo(tmp_path: Path) -> Path:
    """A repo whose first commit already carries the checker, with one clean module."""
    repo = _init(tmp_path / "repo")
    _write(repo, {"pkg/a.py": "def a():\n    return 1\n"})
    staged = _add_engine(repo)
    _git(repo, "add", "--all", "--", ".gitignore", "pyproject.toml", "pkg", *staged)
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _check(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return _sh(repo, sys.executable, str(repo / "scripts/check"), *args)


def _timeless(report: str) -> str:
    """A report minus its elapsed time (`(0.1s)`), the one part two identical runs may differ in."""
    return re.sub(r"\(\d+\.\ds\)", "", report)


def _ratchet_repo(tmp_path: Path, switch: str | None = None) -> tuple[Path, str]:
    repo = _init(tmp_path / "repo")
    (repo / "scripts/ci").mkdir(parents=True)
    shutil.copy2(REPO / "scripts/ci/profile_scope_patterns.json", repo / "scripts/ci/")
    files: dict[str, str | None] = {"pkg/a.py": _LEGACY, **({_SWITCH: switch} if switch else {})}
    _write(repo, files)
    _git(repo, "add", "--all", "--", ".gitignore", "pyproject.toml", "scripts", "pkg")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


# --- F20: the staged/commit health verdict comes from the judged artifact only -----------------


@pytest.mark.parametrize("unstaged", [
    # the policy file read by the PS-P05 rule, narrowed so it no longer covers pkg/
    ("scripts/ci/profile_scope_patterns.json", lambda t: t.replace(
        '"id": "P05",', '"id": "P05",\n      "path_regex": "^nowhere/",')),
    # the engine itself, edited to exempt pkg/ from PS-P05
    ("scripts/code_health/config.py", lambda t: t.replace(
        'exclude=SINGLE_PROFILE, blocking=WARN_UNTIL_REPLAYED, pattern_id="P05"',
        'exclude=SINGLE_PROFILE + ("pkg/*",), blocking=WARN_UNTIL_REPLAYED, pattern_id="P05"')),
])
def test_staged_health_ignores_unstaged_policy_and_engine(tmp_path, unstaged):
    repo = _engine_repo(tmp_path)
    _write(repo, {"pkg/c.py": _ENV_COPY})
    _git(repo, "add", "--", "pkg/c.py")
    tree = _git(repo, "write-tree")
    first = _check(repo, "--staged", "--only", "health", "--base", "HEAD")
    assert first.returncode == 0 and "PS-P05" in first.stdout, first.stdout + first.stderr

    rel, edit = unstaged
    original = (repo / rel).read_text(encoding="utf-8")
    edited = edit(original)
    assert edited != original
    (repo / rel).write_text(edited, encoding="utf-8")
    again = _check(repo, "--staged", "--only", "health", "--base", "HEAD")
    assert _timeless(again.stdout) == _timeless(first.stdout), again.stdout + again.stderr
    # the judged artifact and the user's unstaged work are both untouched
    assert _git(repo, "write-tree") == tree
    assert (repo / rel).read_text(encoding="utf-8") == edited
    assert _git(repo, "diff", "--name-only") == rel


# --- F22: selectors are validated before anything runs ----------------------------------------


@pytest.mark.parametrize("selector", ["heath", ",", "", "heath,rof", "health,rof"])
def test_unknown_or_empty_selector_is_a_usage_error(selector):
    proc = _sh(REPO, sys.executable, str(REPO / "scripts/check"), "--only", selector)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "health" in proc.stderr and "shebangs" in proc.stderr  # lists the valid names
    assert "checks, ok" not in proc.stdout


def test_valid_selector_runs_its_check_and_keeps_its_status(tmp_path):
    repo = _engine_repo(tmp_path)
    _write(repo, {"pkg/c.py": _SWALLOW})
    _git(repo, "add", "--", "pkg/c.py")
    proc = _check(repo, "--staged", "--only", "health,shebangs", "--base", "HEAD")
    assert proc.returncode == 1 and "2 checks, FAILED: health" in proc.stdout, proc.stdout


# --- JSON output on every return path ----------------------------------------------------------


@pytest.mark.parametrize("case, switch, files, code, findings", [
    ("no measured change", None, {"README.md": "docs\n"}, 0, 0),
    ("enforcement off", 'ENFORCEMENT = "off"\n', {"pkg/a.py": _GROWN}, 0, 0),
    ("findings", None, {"pkg/a.py": _GROWN}, 1, 1),
    ("clean measured diff", None, {"pkg/b.py": "def b():\n    return 2\n"}, 0, 0),
])
def test_json_stdout_is_json_on_every_path(tmp_path, capsys, case, switch, files, code, findings):
    repo, base = _ratchet_repo(tmp_path, switch)
    head = _commit(repo, files)
    assert cli.run(repo, base, head, as_json=True) == code, case
    out, err = capsys.readouterr()
    data = json.loads(out)
    assert isinstance(data, list) and len(data) == findings, (case, out)
    if case in ("no measured change", "enforcement off"):
        assert "code health:" in err  # the human explanation moves to stderr


# --- pre-push: the pushed candidate decides, never the checked-out branch ----------------------


def _pushable(tmp_path: Path) -> tuple[Path, Path]:
    """`main` has the guards but predates scripts/check, and is published: (repo, bare remote)."""
    repo = _init(tmp_path / "repo")
    _write(repo, {"pkg/a.py": "def a():\n    return 1\n"})
    staged = _add_engine(repo, checker=False)
    _git(repo, "add", "--all", "--", ".gitignore", "pyproject.toml", "pkg", *staged)
    _git(repo, "commit", "-q", "-m", "main before scripts/check")
    (repo / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    return repo, remote


def _branch_with_checker(repo: Path, name: str, extra: dict[str, str]) -> str:
    _git(repo, "checkout", "-q", "-b", name, "main")
    _write(repo, extra)
    _git(repo, "add", "--all", "--", *_add_engine(repo), *extra)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _remote_ref(remote: Path, branch: str) -> str:
    return _git(remote, "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}")


def test_pre_push_judges_a_checker_branch_pushed_from_an_older_checkout(tmp_path):
    repo, remote = _pushable(tmp_path)
    _branch_with_checker(repo, "feature", {"pkg/c.py": _SWALLOW})
    good = _branch_with_checker(repo, "clean", {"pkg/d.py": "def d():\n    return 4\n"})
    assert _check(repo, "--install-hook", "pre-push").returncode == 0
    _git(repo, "checkout", "-q", "main")
    assert not (repo / "scripts/check").exists()

    push = _sh(repo, "git", "push", "origin", "feature")
    assert push.returncode != 0, push.stdout + push.stderr
    assert "BLE001" in push.stdout + push.stderr
    assert _remote_ref(remote, "feature") == ""  # destination untouched

    # controls: a clean checker branch passes, a branch predating the checker is not judged,
    # a deletion is not judged
    push = _sh(repo, "git", "push", "origin", "clean")
    assert push.returncode == 0 and _remote_ref(remote, "clean") == good, push.stderr
    _git(repo, "checkout", "-q", "-b", "old", "main")
    old = _commit(repo, {"pkg/c.py": _SWALLOW})
    assert _sh(repo, "git", "push", "origin", "old").returncode == 0
    assert _remote_ref(remote, "old") == old
    assert _sh(repo, "git", "push", "origin", "--delete", "clean").returncode == 0
    assert _remote_ref(remote, "clean") == ""


def test_pre_push_refuses_an_untested_upstream_sync_on_any_branch(tmp_path):
    """The 10-29 sync reached main by hand pushes; now only the CI gate pushes upstream commits."""
    repo, remote = _pushable(tmp_path)
    fork = _branch_with_checker(repo, "fork-main", {"pkg/d.py": "def d():\n    return 4\n"})
    _git(repo, "push", "-q", "origin", "fork-main:main")
    _git(repo, "fetch", "-q", "origin")
    upstream = tmp_path / "upstream.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(upstream))
    _git(repo, "push", "-q", str(upstream), "main:main")  # upstream: main predating the fork commit
    up_work = tmp_path / "up-work"
    _git(tmp_path, "clone", "-q", str(upstream), str(up_work))
    _git(up_work, "config", "user.email", "u@example.invalid")
    _git(up_work, "config", "user.name", "u")
    _commit(up_work, {"pkg/up.py": "def up():\n    return 5\n"})
    _git(up_work, "push", "-q", "origin", "HEAD:main")
    _git(repo, "remote", "add", "upstream", str(upstream))
    _git(repo, "fetch", "-q", "upstream")
    assert _check(repo, "--install-hook", "pre-push").returncode == 0

    _git(repo, "checkout", "-q", "-b", "sync", fork)
    _git(repo, "merge", "-q", "--no-edit", "refs/remotes/upstream/main")
    sync = _git(repo, "rev-parse", "HEAD")
    for ref in ("main", "sync"):  # by hand, to main or to a branch that a PR would merge
        push = _sh(repo, "git", "push", "origin", f"{sync}:refs/heads/{ref}")
        assert push.returncode != 0 and "upstream sync guard" in push.stderr, push.stdout + push.stderr
    assert _remote_ref(remote, "main") == fork and _remote_ref(remote, "sync") == ""

    # The gate's own staging push passes; fork-only work passes.
    env = {**_env(), "HERMES_UPSTREAM_SYNC_GATE": "stage"}
    push = _sh(repo, "git", "push", "origin", f"{sync}:refs/heads/sync/upstream-{sync}", env=env)
    assert push.returncode == 0, push.stdout + push.stderr
    _git(repo, "checkout", "-q", "-b", "feature", fork)
    work = _commit(repo, {"pkg/e.py": "def e():\n    return 6\n"})
    push = _sh(repo, "git", "push", "origin", "feature")
    assert push.returncode == 0 and _remote_ref(remote, "feature") == work, push.stdout + push.stderr


# --- M5: interpreter choice and the version guard ---------------------------------------------


def test_hooks_prefer_the_repo_virtualenv_over_path_python(tmp_path):
    repo = _engine_repo(tmp_path)
    assert _check(repo, "--install-hook", "pre-commit").returncode == 0
    fake = tmp_path / "fakebin"
    fake.mkdir()
    for name in ("python3", "python"):
        (fake / name).write_text("#!/bin/sh\necho 'wrong interpreter' >&2\nexit 97\n",
                                 encoding="utf-8")
        (fake / name).chmod(0o755)
    env = _env()
    env["PATH"] = f"{fake}{os.pathsep}{env['PATH']}"
    (repo / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    _write(repo, {"pkg/b.py": "def b():\n    return 2\n"})
    _git(repo, "add", "--", "pkg/b.py")
    proc = _sh(repo, "git", "commit", "-q", "-m", "venv", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Without a virtualenv a python3 that cannot run scripts/check is passed over, not used:
    # test_hooks_without_a_virtualenv_pass_over_a_path_python3_older_than_3_11.


def _shim(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _old_python3(fake: Path) -> None:
    """macOS's /usr/bin/python3 (3.9) as the hook sees it: fails the 3.11 probe, still decodes
    JSON (as any python does), and can run nothing of scripts/check."""
    for name in ("python3", "python"):
        _shim(fake / name, 'case "$2" in\n  *version_info*) exit 1 ;;\n'
              f"  *json.load*) exec '{sys.executable}' \"$@\" ;;\nesac\n"
              "echo 'wrong interpreter' >&2\nexit 1")


def _recording_python(path: Path, record: Path) -> Path:
    return _shim(path, f"echo \"$0\" >> '{record}'\nexec '{sys.executable}' \"$@\"")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shims stand in for the interpreters")
def test_hooks_without_a_virtualenv_pass_over_a_path_python3_older_than_3_11(tmp_path):
    """The Macs' pm-managed checkouts have no venv/: the hook used to run whatever python3 came
    first on PATH (an agent shell's /usr/bin/python3 3.9), so scripts/check refused every push.
    Now it takes the first python on PATH that is 3.11+, under a versioned name too."""
    repo = _engine_repo(tmp_path)
    assert _check(repo, "--install-hook", "pre-commit").returncode == 0
    _old_python3(tmp_path / "old")
    record = tmp_path / "seen"
    newer = _recording_python(tmp_path / "new bin" / "python3.14", record)  # a space, as in "Hermes Python.app"
    env = _env()
    env["HERMES_HOME"] = str(tmp_path / "hermes-home")  # no installed launcher to borrow
    env["PATH"] = os.pathsep.join([str(tmp_path / "old"), str(newer.parent), env["PATH"]])
    _write(repo, {"pkg/b.py": "def b():\n    return 2\n"})
    _git(repo, "add", "--", "pkg/b.py")
    proc = _sh(repo, "git", "commit", "-q", "-m", "path", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "wrong interpreter" not in proc.stderr and "3.11" not in proc.stderr, proc.stderr
    assert str(newer) in record.read_text(encoding="utf-8").splitlines()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shims stand in for the interpreters")
def test_hooks_with_no_python_3_11_anywhere_still_run_the_bare_python3(tmp_path):
    """Last resort: nothing passes the probe, so PATH's python3 runs scripts/check, whose own
    guard then explains the 3.11 requirement (test_check_refuses_old_python_before_importing_anything)."""
    repo = _engine_repo(tmp_path)
    assert _check(repo, "--install-hook", "pre-commit").returncode == 0
    old = tmp_path / "old"
    _old_python3(old)
    for tool in ("git", "bash", "mktemp", "rm", "sed"):  # what the hook runs, and no other python
        (old / tool).symlink_to(shutil.which(tool))
    env = _env()
    env["HERMES_HOME"] = str(tmp_path / "hermes-home")
    env["PATH"] = str(old)
    _write(repo, {"pkg/b.py": "def b():\n    return 2\n"})
    _git(repo, "add", "--", "pkg/b.py")
    proc = _sh(repo, "git", "commit", "-q", "-m", "old", env=env)
    assert proc.returncode != 0 and "wrong interpreter" in proc.stderr, proc.stdout + proc.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shims stand in for the interpreters")
@pytest.mark.parametrize("where, store", [
    ("main_checkout", "store"), ("installed_hermes", "store"),
    ("main_checkout", "st\u00f6re python"),  # json.dumps escapes it: \\u00f6
])
def test_hooks_without_a_virtualenv_take_the_launchers_interpreter(tmp_path, where, store):
    """A pm-managed checkout's .hermes/bin/hermes names the pinned interpreter
    (`--print-runtime-command`, a JSON list); the hook runs that before searching PATH."""
    repo = _engine_repo(tmp_path)
    assert _check(repo, "--install-hook", "pre-commit").returncode == 0
    _old_python3(tmp_path / "old")
    record = tmp_path / "seen"
    pinned = _recording_python(tmp_path / store / "python3", record)
    hermes_home = tmp_path / "hermes-home"
    root = repo if where == "main_checkout" else hermes_home / "hermes-agent"
    argv = json.dumps([str(pinned), "-I", "-c", "pass"])  # what hermes_cli/_launchers.py prints
    assert "'" not in argv
    _shim(root / ".hermes/bin/hermes", f"[ \"$1\" = --print-runtime-command ] || exit 64\nprintf '%s\\n' '{argv}'")
    env = _env()
    env["HERMES_HOME"] = str(hermes_home)
    env["PATH"] = os.pathsep.join([str(tmp_path / "old"), env["PATH"]])
    _write(repo, {"pkg/b.py": "def b():\n    return 2\n"})
    _git(repo, "add", "--", "pkg/b.py")
    proc = _sh(repo, "git", "commit", "-q", "-m", "launcher", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "wrong interpreter" not in proc.stderr, proc.stderr
    assert str(pinned) in record.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("where", ["main_checkout", "installed_hermes"])
def test_hooks_take_the_pm_composed_environment_minus_its_checkout(tmp_path, where):
    """A pm-managed install (the Macs) keeps the dependencies on its composed PYTHONPATH: the
    stale venv/ python cannot import them, so the hook runs the composed PATH's python with the
    composed PYTHONPATH, minus the composing checkout (the judged tree's own modules must win).
    A clone that is not an install (the sync mirror's worktrees) borrows the installed Hermes
    checkout's environment, ``$HERMES_HOME/hermes-agent``."""
    repo = _engine_repo(tmp_path)
    assert _check(repo, "--install-hook", "pre-commit").returncode == 0
    (repo / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    hermes_home = tmp_path / "hermes-home"
    pm_root = repo if where == "main_checkout" else hermes_home / "hermes-agent"
    record = tmp_path / "seen"
    composed = tmp_path / "composed-bin"
    composed.mkdir()
    (composed / "python3").write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$PYTHONPATH\" >> '{record}'\nexec '{sys.executable}' \"$@\"\n",
        encoding="utf-8")
    (composed / "python3").chmod(0o755)
    env = _env()
    env["HERMES_HOME"] = str(hermes_home)
    path = os.pathsep.join([str(composed), env["PATH"]])
    # untracked in the repo: the hook reads it from the composing checkout, not the judged tree
    _write(pm_root, {"pm/environments.py": (
        "import json, os\n"
        f"print(json.dumps({{'PATH': {path!r}, 'PYTHONPATH': os.pathsep.join([os.getcwd(), '/deps'])}}))\n"
    )})
    _write(repo, {"pkg/b.py": "def b():\n    return 2\n"})
    _git(repo, "add", "--", "pkg/b.py")
    proc = _sh(repo, "git", "commit", "-q", "-m", "pm", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert record.read_text(encoding="utf-8").splitlines() == ["/deps"]
    # control: with no composing checkout the hook keeps the venv python and never runs ours
    (pm_root / "pm/environments.py").unlink()
    record.unlink()
    _write(repo, {"pkg/b.py": "def b():\n    return 3\n"})
    _git(repo, "add", "--", "pkg/b.py")
    proc = _sh(repo, "git", "commit", "-q", "-m", "venv", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not record.exists()


@pytest.mark.parametrize("output", ["", "not json\n", "[1, 2]\n"])
def test_hooks_skip_a_pm_environment_that_yields_no_json_without_a_traceback(tmp_path, output):
    """An interpreter that cannot run pm.environments (the mini's CLT python3.9 in a checkout
    with no virtualenv) prints nothing, or garbage: the hook skips that composing checkout
    quietly instead of printing a JSONDecodeError traceback that reads as a failed push."""
    repo = _engine_repo(tmp_path)
    assert _check(repo, "--install-hook", "pre-commit").returncode == 0
    (repo / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    env = _env()
    env["HERMES_HOME"] = str(tmp_path / "hermes-home")
    _write(repo, {"pm/environments.py": (
        f"import sys\nsys.stdout.write({output!r})\nraise SystemExit(1)\n")})
    _write(repo, {"pkg/b.py": "def b():\n    return 2\n"})
    _git(repo, "add", "--", "pkg/b.py")
    proc = _sh(repo, "git", "commit", "-q", "-m", "pm", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr and "Error" not in proc.stderr, proc.stderr


def test_check_refuses_old_python_before_importing_anything():
    # The guard reads sys.version_info; a 3.10 interpreter is emulated by overriding it before
    # scripts/check runs (tomllib, imported via ruff_runner, would otherwise crash it).
    code = ("import runpy, sys; sys.version_info = (3, 10, 12, 'final', 0); "
            f"sys.argv = ['scripts/check', '--only', 'shebangs']; "
            f"runpy.run_path({str(REPO / 'scripts/check')!r}, run_name='__main__')")
    proc = _sh(REPO, sys.executable, "-c", code)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "3.11" in proc.stderr and "Traceback" not in proc.stderr


# --- m3: the ENFORCEMENT switch ---------------------------------------------------------------


@pytest.mark.parametrize("switch, code", [
    ('ENFORCEMENT: str = "off"\n', 0),
    ('ENFORCEMENT: str = "advisory"\n', 0),
    ('ENFORCEMENT = "warn"\n', 1),  # an unknown value is not a way to relax the check
    ('ENFORCEMENT = "off"  # until the burn-down lands\n', 0),
])
def test_enforcement_switch_parses_annotated_and_unknown_values(tmp_path, capsys, switch, code):
    repo, base = _ratchet_repo(tmp_path, switch)
    head = _commit(repo, {"pkg/a.py": _GROWN})
    assert cli.run(repo, base, head) == code, capsys.readouterr().out


def test_enforcement_reads_this_trees_own_switch():
    assert cli.enforcement(REPO, "HEAD") == ENFORCEMENT
    text = (REPO / _SWITCH).read_text(encoding="utf-8")
    assert cli.parse_switch(text) == ENFORCEMENT


def _branch_behind_main(tmp_path: Path, main_switch: str, branch_files: dict[str, str]) -> Path:
    repo, base = _ratchet_repo(tmp_path, 'ENFORCEMENT = "blocking"\n')
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, branch_files)
    _git(repo, "checkout", "-q", "main")
    tip = _commit(repo, {_SWITCH: main_switch, "NEWS.md": "main moved on\n"})
    _git(repo, "update-ref", "refs/remotes/origin/main", tip)
    _git(repo, "reset", "-q", "--hard", base)  # local main is stale; only origin/main moved
    _git(repo, "checkout", "-q", "feature")
    return repo


def test_local_runs_take_the_switch_from_the_main_tip(tmp_path, monkeypatch, capsys):
    repo = _branch_behind_main(tmp_path, 'ENFORCEMENT = "advisory"\n', {"pkg/a.py": _GROWN})
    monkeypatch.chdir(repo)
    assert cli.main([]) == 0, capsys.readouterr().out  # the flip on main reaches the branch
    head = _git(repo, "rev-parse", "HEAD")
    assert cli.main(["--head", head]) == 0
    out = capsys.readouterr().out
    assert "CC 23 > 22" in out and "advisory mode" in out


def test_a_branch_cannot_relax_its_own_switch(tmp_path, monkeypatch, capsys):
    grown_and_off = {"pkg/a.py": _GROWN, _SWITCH: 'ENFORCEMENT = "off"\n'}
    repo = _branch_behind_main(tmp_path, 'ENFORCEMENT = "blocking"\n', grown_and_off)
    monkeypatch.chdir(repo)
    assert cli.main([]) == 1, capsys.readouterr().out


# --- F23: the hooks under Git for Windows with MSYS path conversion disabled -----------------


def test_hooks_run_with_msys_path_conversion_disabled(tmp_path):
    """Hermes' Windows terminal exports both variables, so native git and python see the hook's
    paths verbatim: a POSIX `/c/...` temp path then reaches them unconverted. Inert elsewhere."""
    repo, remote = _pushable(tmp_path)
    _branch_with_checker(repo, "feature", {"pkg/d.py": "def d():\n    return 4\n"})
    for kind in ("pre-commit", "pre-push"):
        assert _check(repo, "--install-hook", kind).returncode == 0
    env = {**_env(), "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"}

    _write(repo, {"pkg/e.py": _SWALLOW})
    _git(repo, "add", "--", "pkg/e.py")
    commit = _sh(repo, "git", "commit", "-q", "-m", "swallow", env=env)
    assert commit.returncode != 0 and "BLE001" in commit.stdout + commit.stderr, commit.stdout + commit.stderr
    _write(repo, {"pkg/e.py": "def e():\n    return 5\n"})
    _git(repo, "add", "--", "pkg/e.py")
    commit = _sh(repo, "git", "commit", "-q", "-m", "clean", env=env)
    assert commit.returncode == 0, commit.stdout + commit.stderr

    push = _sh(repo, "git", "push", "-q", "origin", "feature", env=env)
    assert push.returncode == 0, push.stdout + push.stderr
    assert _remote_ref(remote, "feature") == _git(repo, "rev-parse", "HEAD")
    _write(repo, {"pkg/f.py": _SWALLOW})
    _git(repo, "add", "--", "pkg/f.py")
    _git(repo, "commit", "-q", "--no-verify", "-m", "swallow")
    push = _sh(repo, "git", "push", "-q", "origin", "feature", env=env)
    assert push.returncode != 0 and "BLE001" in push.stdout + push.stderr, push.stdout + push.stderr


# --- m4: the CI guards that ran outside the lint workflow --------------------------------------


@pytest.mark.parametrize("job, files", [
    ("case-collisions", {"pkg/Notes.md": "a\n", "pkg/NOTES.md": "b\n"}),
    ("lazy-deps", {"pkg/uses.py": "import tools.lazy_deps\n"}),
    ("profile-archives", {"pkg/export.tar.gz": "not really\n"}),
    ("blob-size", {"pkg/shot.png": "\0" * (600 * 1024)}),
])
def test_ci_only_guards_run_in_scripts_check(tmp_path, job, files):
    repo = _engine_repo(tmp_path)
    clean = _check(repo, "--staged", "--only", job, "--base", "HEAD")
    assert clean.returncode == 0 and "1 checks, ok" in clean.stdout, clean.stdout + clean.stderr
    # Staged as blobs, never written to disk: a case-insensitive filesystem holds one of
    # Notes.md / NOTES.md, but the index (what --staged judges) holds both.
    for rel, text in files.items():
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, env=_env(),
                              input=text.encode("utf-8"), capture_output=True, timeout=60, check=True)
        _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob.stdout.decode().strip()},{rel}")
    proc = _check(repo, "--staged", "--only", job, "--base", "HEAD")
    assert proc.returncode == 1 and f"FAILED: {job}" in proc.stdout, proc.stdout + proc.stderr


# --- F21: profile regex rules see executable code, not prose -----------------------------------


_FUNC = "import os\n\n\ndef build(cmd):\n{}    return cmd\n"


@pytest.mark.parametrize("body, flagged", [
    ("    # Avoid env = os.environ.copy(); use the scoped builder.\n", False),
    ('    """Avoid env = os.environ.copy(); use the scoped builder."""\n', False),
    ('    """Builder.\n\n    Never env = os.environ.copy() here.\n    """\n', False),
    ("    # don't read os.getenv(\"DISCORD_TOKEN\") directly\n", False),
    ("    cmd = [cmd]  # not os.environ.get('SLACK_TOKEN')\n", False),
    ("    'inert example: env = os.environ.copy()'\n", False),
    # positive controls: the real operations, including a literal env-key argument
    ("    env = os.environ.copy()\n    cmd = (cmd, env)\n", True),
    ("    token = os.getenv(\"DISCORD_TOKEN\")\n    cmd = (cmd, token)\n", True),
    ("    env = dict(os.environ)  # copy it\n    cmd = (cmd, env)\n", True),
])
def test_profile_regex_rules_ignore_comments_and_docstrings(tmp_path, capsys, body, flagged):
    repo, base = _ratchet_repo(tmp_path)
    head = _commit(repo, {"pkg/c.py": _FUNC.format(body)})
    code = cli.run(repo, base, head)
    out = capsys.readouterr().out
    assert code == 0, out  # PS-P05/P06 are warnings until replayed
    assert ("PS-P05" in out or "PS-P06" in out) == flagged, out


# --- allow comments may list several rules ----------------------------------------------------


@pytest.mark.parametrize("rules", ["BLE001 S110", "BLE001, S110", "BLE001,S110"])
def test_allow_comment_lists_rules_by_comma_or_space(tmp_path, capsys, rules):
    repo, base = _ratchet_repo(tmp_path)
    swallow = ("def other():\n    try:\n        pass\n"
               f"    except Exception:  # health: allow {rules} -- boundary\n        pass\n")
    head = _commit(repo, {"pkg/b.py": swallow})
    assert cli.run(repo, base, head) == 0, capsys.readouterr().out


# --- measurement tooling failures are errors, not findings ------------------------------------


def test_tooling_failure_exits_2_without_a_traceback(tmp_path, monkeypatch, capsys):
    repo, base = _ratchet_repo(tmp_path)
    manifest = {"devDependencies": {"typescript": "0.0.0-not-published"}}
    _commit(repo, {"package.json": json.dumps(manifest), "web/a.ts": "export const a = 1;\n"})
    if not shutil.which("npm") or not shutil.which("node"):
        pytest.skip("needs node + npm to reach the npm install path")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("npm_config_offline", "true")  # a real npm failure, without the network
    monkeypatch.chdir(repo)
    assert cli.main(["--base", base, "--head", "HEAD"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("code health:") and "npm" in err


# --- a merge is judged against each parent (upstream syncs)  [daveey] --------------------------


def _merge_repo(tmp_path: Path, merged: str) -> tuple[Path, str, str, str]:
    """main (parent 1) and a side branch (parent 2) both start from _LEGACY; the side branch
    grows pkg/a.py to _GROWN. Returns the repo, both parents and a merge whose pkg/a.py is
    ``merged``."""
    repo, root = _ratchet_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "side")
    side = _commit(repo, {"pkg/a.py": _GROWN})
    _git(repo, "checkout", "-q", "main")
    first = _commit(repo, {"pkg/other.py": "def other():\n    return 3\n"})
    _git(repo, "merge", "--no-commit", "--no-ff", "-X", "theirs", "side")
    _write(repo, {"pkg/a.py": merged})
    _git(repo, "add", "pkg/a.py")
    _git(repo, "commit", "-q", "-m", "merge")
    assert root and len(_git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3
    return repo, first, side, _git(repo, "rev-parse", "HEAD")


def test_merge_growth_within_one_parents_cap_does_not_block(tmp_path, capsys):
    repo, first, side, head = _merge_repo(tmp_path, _GROWN)
    assert cli.run(repo, first, head) == 1  # against parent 1 alone it blocks
    capsys.readouterr()
    assert cli.run(repo, first, head, also_bases=(side,)) == 0, capsys.readouterr().out


def test_merge_growth_over_both_parents_blocks(tmp_path, capsys):
    repo, first, side, head = _merge_repo(tmp_path, _GROWN + "    if x == 100:\n        return 100\n")
    assert cli.run(repo, first, head, also_bases=(side,)) == 1, capsys.readouterr().out


def test_non_merge_commit_is_judged_against_its_base_only(tmp_path, capsys):
    repo, base = _ratchet_repo(tmp_path)
    head = _commit(repo, {"pkg/a.py": _GROWN})
    assert cli.run(repo, base, head) == 1
    assert gitio.merge_parents(repo, head) == []
    capsys.readouterr()


def test_main_uses_a_merge_commits_parents_automatically(tmp_path, monkeypatch, capsys):
    repo, _first, _side, head = _merge_repo(tmp_path, _GROWN)
    monkeypatch.chdir(repo)
    assert cli.main(["--head", head]) == 0, capsys.readouterr().out
    bad = _commit(repo, {"pkg/a.py": _GROWN + "    if x == 100:\n        return 100\n"})
    assert gitio.merge_parents(repo, bad) == []
    assert cli.main(["--head", bad, "--base", head]) == 1
