"""The never-a-workspace roots are resolved once per input set, not once per path. [daveey]"""
from __future__ import annotations

import tempfile

import pytest

from tui_gateway import server


@pytest.fixture
def homes(tmp_path, monkeypatch):
    import hermes_constants

    home = tmp_path / "user"
    internal = home / ".hermes"
    temp = tmp_path / "system-temp"
    for path in (internal, temp):
        path.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(temp))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: internal)
    monkeypatch.setattr(hermes_constants, "_get_platform_default_hermes_home", lambda: internal)
    monkeypatch.setattr(server, "_non_workspace_memo", None)
    return home, internal, temp


def test_many_paths_resolve_the_roots_once(homes, monkeypatch):
    home, internal, temp = homes
    builds = []
    real = server._non_workspace_dirs
    monkeypatch.setattr(server, "_non_workspace_dirs", lambda: builds.append(1) or real())
    paths = [str(home / f"repo-{i}") for i in range(200)]
    assert not any(server._is_non_workspace_path(p) for p in paths)
    assert server._is_non_workspace_path(str(internal / "cache"))
    assert server._is_non_workspace_path(str(temp / "x"))
    assert server._is_non_workspace_path("/")
    assert server._is_non_workspace_path("")
    assert builds == [1]  # was once per asked path, with ~20 root resolutions each


def test_new_profile_home_is_seen_at_once(homes):
    home, internal, _ = homes
    outside = home / "profile-storage"
    outside.mkdir()
    assert not server._is_non_workspace_path(str(outside / "cache"))
    profiles = internal / "profiles"
    profiles.mkdir()
    assert not server._is_non_workspace_path(str(outside / "cache"))
    (profiles / "linked").symlink_to(outside, target_is_directory=True)
    assert server._is_non_workspace_path(str(outside / "cache"))


def test_changed_home_or_temp_root_is_seen_at_once(homes, monkeypatch, tmp_path):
    import hermes_constants

    home, internal, _ = homes
    other = tmp_path / "other-home"
    other.mkdir()
    assert not server._is_non_workspace_path(str(other / "x"))
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: other)
    assert server._is_non_workspace_path(str(other / "x"))
    env_temp = tmp_path / "env-temp"
    env_temp.mkdir()
    assert not server._is_non_workspace_path(str(env_temp / "y"))
    monkeypatch.setenv("TMPDIR", str(env_temp))
    assert server._is_non_workspace_path(str(env_temp / "y"))


def test_relinked_root_is_seen_after_the_ttl(homes, monkeypatch, tmp_path):
    import hermes_constants

    home, internal, _ = homes
    first, second = tmp_path / "store-1", tmp_path / "store-2"
    first.mkdir()
    second.mkdir()
    link = tmp_path / "hermes-link"
    link.symlink_to(first, target_is_directory=True)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: link)
    assert server._is_non_workspace_path(str(first / "x"))
    link.unlink()
    link.symlink_to(second, target_is_directory=True)
    now = server.time.monotonic()
    monkeypatch.setattr(server.time, "monotonic", lambda: now + server._NON_WORKSPACE_TTL_S + 1)
    assert server._is_non_workspace_path(str(second / "x"))
    assert not server._is_non_workspace_path(str(first / "x"))
