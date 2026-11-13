"""[daveey] RESTART_IGNORE_GLOBS and range_restart_free (t_01a1d471b754e5bc): which checkout moves owe no restart."""

import ast
import subprocess
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
