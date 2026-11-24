"""warm_roots only fans out cwds whose roots are not already cached. [daveey]"""
from __future__ import annotations

import os

import pytest

from tui_gateway import git_probe


@pytest.fixture
def probes(monkeypatch):
    git_probe.invalidate()
    calls = []
    repos = {}

    def run_git(cwd, *args):
        calls.append((cwd, args))
        root = repos.get(cwd, "")
        if not root:
            return ""
        return root if "--show-toplevel" in args else os.path.join(root, ".git")

    monkeypatch.setattr(git_probe, "run_git", run_git)
    pools = []
    real = git_probe.ThreadPoolExecutor

    class Pool(real):
        def __init__(self, *a, **k):
            pools.append(k.get("max_workers") or (a[0] if a else None))
            super().__init__(*a, **k)

    monkeypatch.setattr(git_probe, "ThreadPoolExecutor", Pool)
    yield calls, repos, pools
    git_probe.invalidate()


def test_second_warm_of_the_same_cwds_spawns_no_pool_and_no_git(probes, tmp_path):
    calls, repos, pools = probes
    repo, plain, other = (tmp_path / n for n in ("repo", "plain", "other"))
    for d in (repo, plain, other):
        d.mkdir()
    repos[str(repo)] = str(repo)
    cwds = [str(repo), str(plain), str(other)]
    git_probe.warm_roots(cwds)
    assert pools == [3] and calls
    assert git_probe.resolve(str(repo)) == {"repo_root": str(repo), "worktree_root": str(repo)}
    assert git_probe.resolve(str(plain)) is None
    calls.clear()
    pools.clear()
    git_probe.warm_roots(cwds)
    assert pools == [] and calls == []


def test_only_the_new_cwd_is_probed(probes, tmp_path):
    calls, _repos, pools = probes
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    git_probe.warm_roots([str(old)])
    calls.clear()
    pools.clear()
    git_probe.warm_roots([str(old), str(new)])
    assert pools == []  # one pending cwd resolves inline
    assert {cwd for cwd, _ in calls} == {str(new)}


def test_negative_is_reprobed_when_the_dir_changes_or_the_ttl_ends(probes, tmp_path, monkeypatch):
    calls, repos, _pools = probes
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    git_probe.warm_roots([str(a), str(b)])
    calls.clear()
    (a / ".git").mkdir()  # `git init`: the dir's own mtime moves
    os.utime(a, ns=(1, 1))
    repos[str(a)] = str(a)
    git_probe.warm_roots([str(a), str(b)])
    assert {cwd for cwd, _ in calls} == {str(a)}
    assert git_probe.repo_root(str(a)) == str(a)
    calls.clear()
    now = git_probe.time.monotonic()
    monkeypatch.setattr(git_probe.time, "monotonic", lambda: now + git_probe._NEG_TTL + 1)
    git_probe.warm_roots([str(a), str(b)])
    assert {cwd for cwd, _ in calls} == {str(b)}  # the positive stays, the expired negative is asked again


def test_repo_with_unresolved_common_root_is_still_warmed(probes, tmp_path):
    calls, repos, _pools = probes
    repo = tmp_path / "repo"
    repo.mkdir()
    repos[str(repo)] = str(repo)
    assert git_probe.repo_root(str(repo)) == str(repo)  # toplevel cached, common root not yet
    calls.clear()
    git_probe.warm_roots([str(repo)])
    assert any("--git-common-dir" in args for _, args in calls)
