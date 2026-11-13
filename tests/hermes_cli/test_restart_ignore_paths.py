"""[daveey] RESTART_IGNORE_GLOBS and range_restart_free (t_01a1d471b754e5bc): which checkout moves owe no restart."""

import ast
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gateway import code_skew
from hermes_cli import restart_ignore_paths as rip


@pytest.mark.parametrize("path", [
    "tests/test_x.py", "tests/gateway/conftest.py", "docs/a b.md", ".github/workflows/ci.yml",
    "README.md", "README.zh-CN.md", "CONTRIBUTING.md", "SECURITY.es.md", ".mailmap",
])
def test_ignored_paths(path):
    assert rip.path_restart_ignored(path)


@pytest.mark.parametrize("path", [
    # runtime inputs that only look like docs
    "skills/productivity/notion/SKILL.md", "agent/prompts/system.md", "AGENTS.md", "SOUL.md",
    "plugins/x/README.md", "tui_gateway/tests/test_x.py", "hermes_cli/docs/x.py",
    # code and build inputs
    "gateway/run.py", "pyproject.toml", "uv.lock", "apps/desktop/src/app.ts", "tests", "docs", "testsx/a.py",
    "", "/tests/a", "tests\\a",
])
def test_runtime_paths(path):
    assert not rip.path_restart_ignored(path)


def test_globs_stay_a_plain_literal_tuple():
    """hermes-sync reads the list from the checkout's HEAD with ast.literal_eval: it must stay a literal."""
    tree = ast.parse(Path(rip.__file__).read_text())
    [value] = [n.value for n in tree.body if isinstance(n, ast.Assign)
               and any(getattr(t, "id", None) == "RESTART_IGNORE_GLOBS" for t in n.targets)]
    assert ast.literal_eval(value) == rip.RESTART_IGNORE_GLOBS
    assert all(isinstance(g, str) for g in rip.RESTART_IGNORE_GLOBS)
    assert not any(g.endswith(".md") or g == "**/*.md" for g in rip.RESTART_IGNORE_GLOBS)


def _git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=repo,
                          check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo, files, msg):
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    base = _commit(tmp_path, {"gateway/run.py": "x = 1\n"}, "base")
    return tmp_path, base


def test_tests_only_range_is_restart_free(repo):
    root, base = repo
    _commit(root, {"tests/test_a.py": "def test(): pass\n"}, "t1")
    head = _commit(root, {"docs/x.md": "x\n", ".github/workflows/ci.yml": "on: push\n", "README.md": "r\n"}, "t2")
    assert rip.range_restart_free(str(root), base, head)


def test_mixed_range_is_not(repo):
    root, base = repo
    _commit(root, {"tests/test_a.py": "def test(): pass\n"}, "t1")
    head = _commit(root, {"gateway/run.py": "x = 2\n"}, "code")
    assert not rip.range_restart_free(str(root), base, head)


@pytest.mark.parametrize("path", ["skills/x/SKILL.md", "agent/prompts/system.md"])
def test_prompt_and_skill_markdown_is_not(repo, path):
    root, base = repo
    head = _commit(root, {path: "prompt\n"}, "prompt")
    assert not rip.range_restart_free(str(root), base, head)


def test_a_revert_pair_over_code_is_not(repo):
    """Every commit counts, not the net tree diff."""
    root, base = repo
    _commit(root, {"gateway/run.py": "x = 2\n"}, "code")
    head = _commit(root, {"gateway/run.py": "x = 1\n"}, "revert")
    assert not rip.range_restart_free(str(root), base, head)


def test_empty_equal_reversed_and_bogus_ranges_are_not(repo):
    root, base = repo
    head = _commit(root, {"tests/test_a.py": "x\n"}, "t1")
    assert not rip.range_restart_free(str(root), base, base)          # nothing moved: no verdict to give
    assert not rip.range_restart_free(str(root), head, base)          # backwards (a downgrade)
    assert not rip.range_restart_free(str(root), "abc1234", head)     # not a full sha
    assert not rip.range_restart_free(str(root), "f" * 40, head)      # unknown commit
    assert not rip.range_restart_free(str(root / "nope"), base, head)  # not a repo


def test_empty_commits_only_are_not(repo):
    root, base = repo
    head = _commit(root, {}, "empty")
    assert not rip.range_restart_free(str(root), base, head)
    head = _commit(root, {"tests/test_a.py": "x\n"}, "t1")
    assert rip.range_restart_free(str(root), base, head)  # an empty commit beside a tests commit is fine


def test_merge_bringing_code_is_not(repo):
    root, base = repo
    _git(root, "checkout", "-q", "-b", "side")
    _commit(root, {"gateway/run.py": "x = 3\n"}, "side code")
    _git(root, "checkout", "-q", "main")
    _commit(root, {"tests/test_a.py": "x\n"}, "t1")
    _git(root, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    head = _git(root, "rev-parse", "HEAD")
    assert not rip.range_restart_free(str(root), base, head)


def test_detect_code_skew_ignores_restart_free_drift(repo, monkeypatch):
    root, base = repo
    tests_only = _commit(root, {"tests/test_a.py": "x\n"}, "t1")
    code = _commit(root, {"gateway/run.py": "x = 9\n"}, "code")
    monkeypatch.setattr(code_skew, "_PROJECT_ROOT", root)
    monkeypatch.setattr(code_skew, "_restart_free_verdicts", {})
    monkeypatch.setattr(code_skew, "_boot_fingerprint", f"git:refs/heads/main:{base}")
    monkeypatch.setattr(code_skew, "_fingerprint", lambda: f"git:refs/heads/main:{tests_only}")
    assert code_skew.detect_code_skew() is None
    monkeypatch.setattr(code_skew, "_fingerprint", lambda: f"git:refs/heads/main:{code}")
    assert code_skew.detect_code_skew() == (base[:10], code[:10])


def test_detect_code_skew_fails_closed_when_git_fails(repo, monkeypatch):
    root, base = repo
    tests_only = _commit(root, {"tests/test_a.py": "x\n"}, "t1")
    monkeypatch.setattr(code_skew, "_PROJECT_ROOT", root)
    monkeypatch.setattr(code_skew, "_restart_free_verdicts", {})
    monkeypatch.setattr(code_skew, "_boot_fingerprint", f"git:refs/heads/main:{base}")
    monkeypatch.setattr(code_skew, "_fingerprint", lambda: f"git:refs/heads/main:{tests_only}")

    monkeypatch.setattr(rip, "_git", lambda *_a, **_k: None)  # git missing / timed out
    assert code_skew.detect_code_skew() == (base[:10], tests_only[:10])


# -- PR #2106 review follow-ups (t_01a1d49d2f2a8ae2) -------------------------------------------------------------


def test_a_gitlink_bump_counts_even_when_config_hides_submodules(repo):
    """``diff.ignoreSubmodules=all`` must not hide a submodule pointer bump from the range listing."""
    root, base = repo
    _git(root, "config", "diff.ignoreSubmodules", "all")
    _git(root, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},vendor/sub")
    _git(root, "commit", "-q", "-m", "add gitlink")
    _git(root, "update-index", "--cacheinfo", f"160000,{'2' * 40},vendor/sub")
    _git(root, "commit", "-q", "-m", "bump gitlink")
    gitlink_only = _git(root, "rev-parse", "HEAD")
    head = _commit(root, {"tests/test_a.py": "x\n"}, "t1")
    assert not rip.range_restart_free(str(root), base, head)
    assert not rip.range_restart_free(str(root), base, gitlink_only)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell shim")
def test_git_is_resolved_through_locate_command(repo, monkeypatch, tmp_path_factory):
    """Like ``boot_pinned_imports``: git comes from ``hermes_platform``'s resolver, not a bare PATH lookup."""
    from hermes_platform.resolver import Candidate, Resolution

    root, base = repo
    head = _commit(root, {"tests/test_a.py": "x\n"}, "t1")
    shim_dir = tmp_path_factory.mktemp("shim")
    log = shim_dir / "calls.log"
    shim = shim_dir / "git-shim"
    shim.write_text(f"#!/bin/sh\necho call >> '{log}'\nexec '{shutil.which('git')}' \"$@\"\n")
    shim.chmod(0o755)
    asked = []

    def resolve(name):
        asked.append(name)
        return Resolution("known_path", (Candidate(str(shim), "known_dir:x", True),))

    monkeypatch.setattr(rip, "locate_command", resolve, raising=False)
    assert rip.range_restart_free(str(root), base, head)
    assert asked and set(asked) == {"git"}
    assert log.read_text().count("call") == 2  # merge-base + log, both through the resolved binary

    monkeypatch.setattr(rip, "locate_command", lambda name: Resolution("missing"), raising=False)
    assert not rip.range_restart_free(str(root), base, head)  # no git: unprovable, fail closed
    assert rip.range_restart_verdict(str(root), base, head) is None


def test_a_transient_git_failure_is_not_cached_as_skew(repo, monkeypatch):
    """A timeout/OSError is no verdict: once git works again the same (boot, disk) pair reads restart-free."""
    root, base = repo
    tests_only = _commit(root, {"tests/test_a.py": "x\n"}, "t1")
    monkeypatch.setattr(code_skew, "_PROJECT_ROOT", root)
    monkeypatch.setattr(code_skew, "_restart_free_verdicts", {})
    monkeypatch.setattr(code_skew, "_boot_fingerprint", f"git:refs/heads/main:{base}")
    monkeypatch.setattr(code_skew, "_fingerprint", lambda: f"git:refs/heads/main:{tests_only}")

    real_git = rip._git
    monkeypatch.setattr(rip, "_git", lambda *_a, **_k: None)  # timed out / OSError
    assert code_skew.detect_code_skew() == (base[:10], tests_only[:10])  # fail closed meanwhile
    monkeypatch.setattr(rip, "_git", real_git)
    assert code_skew.detect_code_skew() is None
    assert code_skew._restart_free_verdicts == {(base, tests_only): True}


def test_import_error_after_a_restart_free_drift_is_no_skew_mix(repo, monkeypatch, caplog):
    """``note_import_error`` follows the same rule as ``detect_code_skew``: a tests-only drift is no mix."""
    root, base = repo
    tests_only = _commit(root, {"tests/test_a.py": "x\n"}, "t1")
    code = _commit(root, {"gateway/run.py": "x = 9\n"}, "code")
    caplog.set_level(logging.WARNING, logger="gateway.code_skew")
    monkeypatch.setattr(code_skew, "_PROJECT_ROOT", root)
    monkeypatch.setattr(code_skew, "_restart_free_verdicts", {})
    monkeypatch.setattr(code_skew, "_import_mix_noted", set())
    monkeypatch.setattr(code_skew, "_boot_fingerprint", f"git:refs/heads/main:{base}")
    monkeypatch.setattr(code_skew, "_fingerprint", lambda: f"git:refs/heads/main:{tests_only}")

    def mix_lines():
        return [r for r in caplog.records if r.getMessage().startswith("code-skew import mix")]

    assert code_skew.note_import_error(ImportError("cannot import name 'x'"), "tool") is False
    assert mix_lines() == []

    monkeypatch.setattr(code_skew, "_fingerprint", lambda: f"git:refs/heads/main:{code}")
    assert code_skew.note_import_error(ImportError("cannot import name 'x'"), "tool") is True
    assert len(mix_lines()) == 1
