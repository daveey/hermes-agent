"""The never-a-workspace roots are resolved once per input set, not once per path. [daveey]"""
from __future__ import annotations

import os
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
    # Kernels with coarse timestamps can give both writes one mtime; production is bounded by the TTL.
    stamp = profiles.stat().st_mtime_ns + 1_000_000
    os.utime(profiles, ns=(stamp, stamp))
    assert server._is_non_workspace_path(str(outside / "cache"))


def test_changed_home_or_temp_root_is_seen_at_once(homes, monkeypatch, tmp_path):
    import hermes_constants

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


def _count_realpath(monkeypatch):
    calls = []
    real = os.path.realpath
    monkeypatch.setattr(server.os.path, "realpath", lambda p, *a, **k: calls.append(str(p)) or real(p, *a, **k))
    return calls


def test_canonical_path_is_resolved_once_per_ttl(homes, monkeypatch):
    home, _, _ = homes
    target = home / "repo"
    target.mkdir()
    monkeypatch.setattr(server, "_canonical_memo", {})
    calls = _count_realpath(monkeypatch)
    first = server._canonical_workspace_path(str(target))
    resolved = len(calls)
    assert resolved >= 1
    assert [server._canonical_workspace_path(str(target)) for _ in range(50)] == [first] * 50
    assert len(calls) == resolved  # the repeats made no filesystem call at all
    assert server._canonical_workspace_path("") == ""


def test_canonical_path_sees_a_relinked_alias_after_the_ttl(homes, monkeypatch):
    home, _, _ = homes
    one, two, alias = home / "one", home / "two", home / "alias"
    one.mkdir()
    two.mkdir()
    alias.symlink_to(one, target_is_directory=True)
    monkeypatch.setattr(server, "_canonical_memo", {})
    assert server._canonical_workspace_path(str(alias)) == os.path.normcase(os.path.realpath(one))
    alias.unlink()
    alias.symlink_to(two, target_is_directory=True)
    assert server._canonical_workspace_path(str(alias)) == os.path.normcase(os.path.realpath(one))  # inside the TTL
    now = server.time.monotonic()
    monkeypatch.setattr(server.time, "monotonic", lambda: now + server._CANONICAL_TTL_S + 1)
    assert server._canonical_workspace_path(str(alias)) == os.path.normcase(os.path.realpath(two))


def test_canonical_memo_is_bounded(homes, monkeypatch):
    monkeypatch.setattr(server, "_canonical_memo", {})
    monkeypatch.setattr(server, "_CANONICAL_MAX", 10)
    for i in range(35):
        server._canonical_workspace_path(f"/nonexistent-hhp/{i}")
    assert len(server._canonical_memo) <= 10


def test_policy_roots_do_not_inherit_a_stale_canonical_answer(homes, monkeypatch, tmp_path):
    """A root also asked about as a path must not make the policy two TTLs stale."""
    import hermes_constants

    first, second = tmp_path / "store-1", tmp_path / "store-2"
    first.mkdir()
    second.mkdir()
    link = tmp_path / "hermes-link"
    link.symlink_to(first, target_is_directory=True)
    monkeypatch.setattr(server, "_canonical_memo", {})
    assert server._canonical_workspace_path(str(link)) == os.path.normcase(os.path.realpath(first))
    link.unlink()
    link.symlink_to(second, target_is_directory=True)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: link)  # new key: the policy is rebuilt now
    assert server._non_workspace_policy()[1].count(os.path.normcase(os.path.realpath(second))) == 1
