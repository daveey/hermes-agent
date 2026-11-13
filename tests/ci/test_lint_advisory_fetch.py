"""lint.yml's advisory diff steps survive git 2.55's promisor-fetch BUG on the partial clone.

"Fetch the merge base" makes the Python-lints checkout a partial clone (--filter=tree:0). The two
advisory steps after it ("Profile-scope patterns on added lines", "Public-surface diff vs base")
used to run their own `git fetch --deepen` on that clone; on git 2.55.0 such a fetch from the
promisor remote dies in index-pack ("BUG: builtin/pack-objects.c:5004 ...", exit 128), and in 11
of ~44 jobs on 2026-11-13 (e.g. run 43455124649) both checks silently never ran. These tests
replay the two real step scripts from lint.yml on such a clone, with a `git` shim that fails a
fetch the way 2.55 does whenever the repo is in promisor mode or the fetch is filtered.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from tests.ci import _gha_expr as gha
from tests.ci.test_fetch_merge_base import GIT_255_BUG, GIT_ENV, PARTIAL_CLONE_KEYS, commit, git

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/actions/fetch-merge-base/fetch-merge-base.sh"
STEPS = ("Profile-scope patterns on added lines (advisory)", "Public-surface diff vs base (advisory)")
ADVISORY_SCRIPTS = {STEPS[0]: "scripts/check_profile_scope_patterns.py",
                    STEPS[1]: "scripts/ci/check_public_surface.py"}

pytestmark = pytest.mark.platforms("posix")

# Fails like git 2.55.0 on the runner: any fetch from a promisor remote (the partial clone's
# remote.origin.promisor, or an explicit --filter that would make it one) prints the BUG text and
# exits 128. LINT_FAIL=all fails every fetch (a fetch that cannot succeed at all).
SHIM = """\
#!/usr/bin/env bash
is_fetch=0; filtered=0
for a in "$@"; do
  [[ $a == fetch ]] && is_fetch=1
  [[ $a == --filter=* ]] && filtered=1
done
if (( is_fetch )); then
  printf '%s\\x1f' "$@" >> "$LINT_ARGV_LOG"; printf '\\n' >> "$LINT_ARGV_LOG"
  promisor=$("$LINT_REAL_GIT" config --get remote.origin.promisor || true)
  if [[ $LINT_FAIL == all ]] || { [[ $LINT_FAIL == promisor ]] && { (( filtered )) || [[ $promisor == true ]]; }; }; then
    "$LINT_REAL_GIT" config remote.origin.promisor true
    "$LINT_REAL_GIT" config remote.origin.partialclonefilter tree:0
    "$LINT_REAL_GIT" config extensions.partialclone origin
    cat "$LINT_BUG_TEXT" >&2
    exit 128
  fi
  if [[ -z $LINT_FAIL ]]; then
    # Healthy mode must succeed on every host git, but the runner's real git 2.55 hits the BUG
    # itself here: answer that one fetch without promisor mode, invisibly to the step.
    err=$(mktemp); rc=0
    "$LINT_REAL_GIT" "$@" 2>"$err" || rc=$?
    if (( rc )) && grep -q should_include_obj "$err"; then
      saved=()
      for k in remote.origin.promisor remote.origin.partialclonefilter extensions.partialclone; do
        saved+=("$k=$("$LINT_REAL_GIT" config --get "$k" || true)")
        "$LINT_REAL_GIT" config --unset "$k" || true
      done
      args=(); for a in "$@"; do [[ $a == --filter=* ]] || args+=("$a"); done
      rc=0; "$LINT_REAL_GIT" "${args[@]}" || rc=$?
      for kv in "${saved[@]}"; do [[ -n ${kv#*=} ]] && "$LINT_REAL_GIT" config "${kv%%=*}" "${kv#*=}"; done
    else
      cat "$err" >&2
    fi
    rm -f "$err"; exit "$rc"
  fi
fi
exec "$LINT_REAL_GIT" "$@"
"""

# Stands in for the setup-pm interpreter: records the advisory script's argv, then proves the
# refs it was given answer a three-dot diff and a blob read on this clone (lazily fetched trees).
FAKE_PYTHON = """\
#!/usr/bin/env bash
{ printf '%s\\x1f' "$@"; printf '\\n'; } > "$LINT_PYTHON_LOG"
base=$3 head=$5
git diff --no-renames --name-only "$base...$head" >> "$LINT_PYTHON_LOG"
git show "$head:f2.txt" >> "$LINT_PYTHON_LOG"
"""


def lint_step(name: str) -> dict:
    job = YAML(typ="safe").load((ROOT / ".github/workflows/lint.yml").read_text(encoding="utf-8"))
    return next(s for s in job["jobs"]["lint"]["steps"] if s.get("name") == name)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """main m1..m4; feature (PR 7) f1, f2 off m2; refs/pull/7/merge = main + feature."""
    repo = tmp_path / "origin"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "uploadpack.allowFilter", "true")
    git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    for i in (1, 2):
        commit(repo, f"m{i}")
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "f1")
    commit(repo, "f2")
    git(repo, "checkout", "-q", "main")
    for i in (3, 4):
        commit(repo, f"m{i}")
    git(repo, "update-ref", "refs/pull/7/head", "feature")
    git(repo, "checkout", "-q", "--detach", "main")
    git(repo, "merge", "-q", "--no-edit", "feature")
    git(repo, "update-ref", "refs/pull/7/merge", "HEAD")
    git(repo, "checkout", "-q", "main")
    return repo


def pr_checkout(origin: Path, tmp_path: Path) -> Path:
    """actions/checkout (fetch-depth 2) of refs/pull/7/merge, then the real 'Fetch the merge base'."""
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q")
    git(work, "remote", "add", "origin", origin.as_uri())
    git(work, "fetch", "-q", "--no-tags", "--depth=2", "origin",
        "+refs/pull/7/merge:refs/remotes/pull/7/merge")
    git(work, "checkout", "-q", "--detach", "refs/remotes/pull/7/merge")
    first = subprocess.run(["bash", str(SCRIPT), "main"], cwd=work, env={**os.environ, **GIT_ENV},
                           capture_output=True, encoding="utf-8", timeout=120, check=False)
    assert first.returncode == 0, first.stderr
    assert git(work, "config", "--get", "remote.origin.promisor") == "true"
    # The step's checkout carries the action script (untracked here, so commits are unchanged).
    action = work / ".github/actions/fetch-merge-base"
    action.mkdir(parents=True)
    shutil.copy2(SCRIPT, action / SCRIPT.name)
    # main moves on while the job runs: the advisory step must fetch, not reuse step 1's refs.
    commit(origin, "m5")
    return work


def run_step(name: str, work: Path, tmp_path: Path, fail: str) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    step = lint_step(name)
    head = git(work.parent / "origin", "rev-parse", "feature")
    ctx = {"github": {"base_ref": "main", "event_name": "pull_request",
                      "event": {"pull_request": {"number": 7, "head": {"sha": head}}}},
           "inputs": {"python": True, "event_name": "pull_request"},
           "steps": {"setup": {"outcome": "success"}},
           "__status__": {"cancelled": False, "success": True, "failure": False, "always": True}}
    assert gha.condition(step["if"], ctx), f"{name}: disabled for a Python pull request"
    bins = tmp_path / f"bin-{fail or 'ok'}-{STEPS.index(name)}"
    bins.mkdir()
    for tool, text in (("git", SHIM), ("python", FAKE_PYTHON)):
        (bins / tool).write_text(text, encoding="utf-8")
        (bins / tool).chmod(0o755)
    argv_log, python_log, bug = bins / "fetch-argv.log", bins / "python.log", bins / "bug.txt"
    argv_log.touch()
    bug.write_text(GIT_255_BUG, encoding="utf-8")
    env = {**os.environ, **GIT_ENV,
           **{k: gha.to_string(gha.render(v, ctx)) for k, v in (step.get("env") or {}).items()},
           "PATH": f"{bins}{os.pathsep}{os.environ['PATH']}", "LINT_REAL_GIT": shutil.which("git"),
           "LINT_FAIL": fail, "LINT_ARGV_LOG": str(argv_log), "LINT_PYTHON_LOG": str(python_log),
           "LINT_BUG_TEXT": str(bug)}
    result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
                             gha.render(step["run"], ctx)],
                            cwd=work, env=env, capture_output=True, encoding="utf-8", timeout=120,
                            check=False)
    return result, argv_log, python_log


def fetches(argv_log: Path) -> list[list[str]]:
    return [line.split("\x1f")[:-1] for line in argv_log.read_text(encoding="utf-8").splitlines()]


def partial_config(work: Path) -> dict[str, str | None]:
    out = {}
    for key in PARTIAL_CLONE_KEYS:
        got = subprocess.run(["git", "config", "--get", key], cwd=work, env={**os.environ, **GIT_ENV},
                             capture_output=True, encoding="utf-8", check=False)
        out[key] = got.stdout.strip() if got.returncode == 0 else None
    return out


@pytest.mark.parametrize("name", STEPS)
def test_step_stays_advisory(name):
    step = lint_step(name)
    assert step["continue-on-error"] is True
    assert 0 < step["timeout-minutes"] <= 3


@pytest.mark.parametrize("name", STEPS)
def test_git_255_bug_on_the_partial_clone_falls_back_and_the_check_runs(name, origin, tmp_path):
    """The incident: the step's fetch on step 1's partial clone dies with the BUG text."""
    work = pr_checkout(origin, tmp_path)
    before = partial_config(work)

    result, argv_log, python_log = run_step(name, work, tmp_path, fail="promisor")

    assert result.returncode == 0, result.stderr
    assert "BUG: builtin/pack-objects.c:5004" in result.stderr
    assert any(line.startswith("fetch-merge-base: filtered fetch failed (rc=128): ")
               and line.endswith("; full-history fallback") for line in result.stderr.splitlines())
    calls = fetches(argv_log)
    assert len(calls) == 2, calls
    assert "--filter=tree:0" in calls[0]
    assert not any(a.startswith("--filter") for a in calls[1])
    assert "::warning" not in result.stdout
    head = git(origin, "rev-parse", "feature")
    argv, *answer = python_log.read_text(encoding="utf-8").splitlines()
    assert argv.split("\x1f")[:-1] == [ADVISORY_SCRIPTS[name], "--base", "origin/main", "--head", head]
    assert answer == ["f1.txt", "f2.txt", "f2"]
    assert git(work, "rev-parse", "refs/remotes/origin/main") == git(origin, "rev-parse", "main")
    assert partial_config(work) == before


@pytest.mark.parametrize("name", STEPS)
def test_a_healthy_fetch_runs_the_check_with_no_fallback(name, origin, tmp_path):
    work = pr_checkout(origin, tmp_path)

    result, argv_log, python_log = run_step(name, work, tmp_path, fail="")

    assert result.returncode == 0, result.stderr
    assert "full-history fallback" not in result.stderr
    assert "::warning" not in result.stdout
    assert len(fetches(argv_log)) == 1
    assert python_log.read_text(encoding="utf-8").splitlines()[1:] == ["f1.txt", "f2.txt", "f2"]


@pytest.mark.parametrize("name", STEPS)
def test_a_fetch_that_cannot_succeed_warns_once_and_skips(name, origin, tmp_path):
    """Still advisory (exit 0), but the skipped check is visible as one ::warning:: line."""
    work = pr_checkout(origin, tmp_path)

    result, argv_log, python_log = run_step(name, work, tmp_path, fail="all")

    assert result.returncode == 0, result.stderr
    warnings = [line for line in result.stdout.splitlines() if line.startswith("::warning")]
    assert len(warnings) == 1, result.stdout
    assert "advisory skipped::" in warnings[0]
    assert len(fetches(argv_log)) == 2  # the filtered fetch and its one fallback
    assert not python_log.exists()
