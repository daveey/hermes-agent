"""Never auto-promote internal paths; explicit project selection remains authoritative."""
from __future__ import annotations

import os
import subprocess
import tempfile

import pytest

from tui_gateway import server


@pytest.fixture
def workspace_paths(tmp_path, monkeypatch):
    import hermes_constants

    home = tmp_path / "user"
    internal = home / ".hermes"
    temp = tmp_path / "system-temp"
    env_temp = tmp_path / "env-temp"
    for path in (home, internal, temp, env_temp):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TMPDIR", str(env_temp))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: internal)
    monkeypatch.setattr(hermes_constants, "_get_platform_default_hermes_home", lambda: internal)
    return home, internal, temp, env_temp


def _tree(monkeypatch, sessions, projects=(), discovered=()):
    monkeypatch.setattr(server, "_project_tree_inputs", lambda *a, **k: (
        sessions, list(projects), list(discovered), None))
    monkeypatch.setattr(server.git_probe, "warm_roots", lambda *a: None)

    def resolve(path):
        root = os.path.realpath(path)
        return {"repo_root": root, "worktree_root": root} if os.path.isdir(
            os.path.join(root, ".git")) else None

    monkeypatch.setattr(server.git_probe, "resolve", resolve)
    return server._build_project_tree(
        None, preview_limit=3, hydrate=True, session_limit=100, include_discovered=True)[0]


@pytest.mark.parametrize("kind", [
    "scratch", "noindex", "scratch-link", "kanban", "profile", "temp", "env-temp",
    "posix-temp", "private-temp", "darwin-temp"])
def test_internal_cwd_stays_home(workspace_paths, monkeypatch, kind):
    _, internal, temp, env_temp = workspace_paths
    paths = {
        "scratch": internal / "cache/scratch",
        "noindex": internal / "cache/scratch.noindex",
        "scratch-link": internal / "cache/scratch",
        "kanban": internal / "cache/scratch.noindex/.worktrees/t_ab12",
        "profile": internal / "profiles/other/cache/scratch",
        "temp": temp / "child", "env-temp": env_temp / "child",
        "posix-temp": "/tmp/x", "private-temp": "/private/tmp/x",
        "darwin-temp": "/var/folders/ab/cache",
    }
    path = paths[kind]
    if not isinstance(path, str):
        if kind == "scratch-link":
            target = paths["noindex"]
            target.mkdir(parents=True)
            path.symlink_to(target, target_is_directory=True)
        else:
            path.mkdir(parents=True)
        if kind == "kanban":
            (path / ".git").mkdir()
    # Synthetic cross-platform roots need not exist on this host.
    monkeypatch.setattr(server, "_dir_exists_cached", lambda p: True)
    row = {"id": kind, "cwd": str(path)}
    tree = _tree(monkeypatch, [row], discovered=[{"root": str(path)}])
    assert [p["id"] for p in tree["projects"]] == ["__no_project__"]
    assert tree["projects"][0]["sessionIds"] == [kind]


def test_controls_explicit_internal_and_symlink_lanes(workspace_paths, monkeypatch):
    home, internal, _, _ = workspace_paths
    repo, prose = home / "repo", home / "notes"
    scratch = internal / "cache/scratch.noindex"
    for path in (repo / ".git", prose, scratch):
        path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    alias = home / "notes-link"
    alias.symlink_to(prose, target_is_directory=True)
    scratch_alias = scratch.with_name("scratch")
    scratch_alias.symlink_to(scratch, target_is_directory=True)
    rows = [{"id": str(i), "cwd": str(path)} for i, path in enumerate(
        (repo, prose, alias, scratch, scratch_alias))]
    explicit = {"id": "explicit", "name": "Chosen internal", "primary_path": str(scratch),
                "folders": [{"path": str(scratch)}]}
    tree = _tree(monkeypatch, rows, [explicit])
    by_id = {p["id"]: p for p in tree["projects"]}
    assert set(by_id) == {"explicit", str(repo), str(prose)}
    assert by_id[str(repo)]["sessionIds"] == ["0"]
    assert by_id[str(prose)]["sessionIds"] == ["1", "2"]
    assert by_id["explicit"]["sessionIds"] == ["3", "4"]
    assert len(by_id["explicit"]["repos"][0]["groups"]) == 1
    assert all(len(p["repos"]) == 1 for p in by_id.values())


def test_discovery_payload_drops_internal_cache(workspace_paths, monkeypatch):
    from hermes_cli import projects_db

    home, internal, _, _ = workspace_paths
    repo = home / "repo"
    repo.mkdir()
    alias = home / "repo-link"
    alias.symlink_to(repo, target_is_directory=True)
    roots = [internal / "cache/scratch", repo, alias]
    monkeypatch.setattr(projects_db, "list_discovered_repos", lambda c: [
        {"root": str(path), "label": path.name} for path in roots])
    result = server._discover_repos_payload(None, conn=object(), backfill=False)
    assert [r["root"] for r in result] == [str(repo)]


def test_profile_scope_still_excludes_sibling_profiles(workspace_paths, monkeypatch):
    import hermes_constants
    home, internal, temp, env_temp = workspace_paths
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: internal / "profiles/current")
    for root in (internal, internal / "profiles/other/cache", temp, env_temp):
        assert server._is_non_workspace_path(str(root))
    for path in (home / ".hermes-other", temp.with_name("system-temp-other")):
        assert not server._is_non_workspace_path(str(path))
    assert server._is_non_workspace_path("")
    # A sibling profile may itself be symlinked outside the default home.
    outside = home / "profile-storage"
    outside.mkdir()
    profiles = internal / "profiles"
    profiles.mkdir()
    (profiles / "linked").symlink_to(outside, target_is_directory=True)
    assert server._is_non_workspace_path(str(profiles / "linked/cache"))
    assert server._is_non_workspace_path(str(outside / "cache"))


def test_internal_worktree_cannot_escape_through_external_git_root(workspace_paths, monkeypatch):
    home, internal, _, _ = workspace_paths
    root, worktree = home / "repo", internal / "worktrees/t_ab12"
    root.mkdir()
    worktree.mkdir(parents=True)
    monkeypatch.setattr(server, "_project_tree_inputs", lambda *a, **k: (
        [{"id": "worker", "cwd": str(worktree), "git_repo_root": str(root)}], [],
        [{"root": str(worktree)}], None))
    monkeypatch.setattr(server.git_probe, "warm_roots", lambda *a: None)
    monkeypatch.setattr(server.git_probe, "resolve", lambda p: {
        "repo_root": str(root), "worktree_root": str(worktree)})
    tree = server._build_project_tree(
        None, preview_limit=3, hydrate=True, session_limit=100, include_discovered=True)[0]
    assert [p["id"] for p in tree["projects"]] == ["__no_project__"]


def test_discovery_ignores_internal_cwd_with_external_root(workspace_paths, monkeypatch):
    from types import SimpleNamespace
    home, internal, _, _ = workspace_paths
    repo, cwd = str(home / "repo"), str(internal / "worktrees/t_ab12")
    db = SimpleNamespace(distinct_session_cwds=lambda: [
        {"cwd": cwd, "sessions": 7}, {"cwd": repo, "sessions": 2}])
    monkeypatch.setattr(server.git_probe, "warm_roots", lambda *a: None)
    monkeypatch.setattr(server.git_probe, "common_repo_root", lambda p: repo)
    result = server._discover_repos_payload(db, backfill=False, include_cached=False)
    assert [(r["root"], r["sessions"]) for r in result] == [(repo, 2)]
