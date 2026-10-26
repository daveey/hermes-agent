"""Regression: the conftest guard catches writes to a checkout's shared .git/config."""

import subprocess
from types import SimpleNamespace

from tests import conftest as guard

import pytest

from tests.conftest import _assert_git_configs_unchanged, _live_git_config_paths, _read_bytes_or_none


def _init(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path / ".git" / "config"


@pytest.mark.parametrize("fake_kind", ["incomplete", "none", "refused"])
@pytest.mark.parametrize("mutate", [False, True])
def test_guard_teardown_with_mocked_popen(tmp_path, monkeypatch, fake_kind, mutate):
    cfg = _init(tmp_path)
    monkeypatch.setattr(guard, "_live_git_config_paths", lambda: [cfg])
    request = SimpleNamespace(node=SimpleNamespace(get_closest_marker=lambda name: None, stash=pytest.Stash()))
    guard._live_git_config_guard.__wrapped__(request)
    if mutate:
        subprocess.run([
            "git", "config", "--file", str(cfg), "user.email", "fixture@example.invalid",
        ], check=True)

    def fake_popen(*args, **kwargs):
        if fake_kind == "refused":
            pytest.fail("test forbids application subprocesses")
        return SimpleNamespace(pid=123) if fake_kind == "incomplete" else None

    def fake_run(*args, **kwargs):
        pytest.fail("guard must not call mocked subprocess.run")

    # Restore mocks even on failure so pytest can render the regression report.
    with monkeypatch.context() as mocks:
        mocks.setattr(subprocess, "Popen", fake_popen)
        mocks.setattr(subprocess, "run", fake_run)
        if mutate:
            with pytest.raises(pytest.fail.Exception, match="test wrote user.* into a live checkout"):
                guard._check_live_git_configs(request.node)
            assert guard._read_bytes_or_none(cfg) == b""
        else:
            guard._check_live_git_configs(request.node)


@pytest.mark.parametrize("mutate", [False, True])
def test_guard_teardown_survives_empty_path(tmp_path, monkeypatch, mutate):
    cfg = _init(tmp_path)
    snapshots = {cfg: _read_bytes_or_none(cfg)}
    if mutate:
        subprocess.run([
            "git", "config", "--file", str(cfg), "user.email", "fixture@example.invalid",
        ], check=True)
    with monkeypatch.context() as mocks:
        mocks.setenv("PATH", "")
        if mutate:
            with pytest.raises(pytest.fail.Exception, match="test wrote user"):
                _assert_git_configs_unchanged(snapshots)
            assert _read_bytes_or_none(cfg) == b""
        else:
            _assert_git_configs_unchanged(snapshots)


def test_guard_watches_the_checkout_under_test():
    assert _live_git_config_paths()


def test_guard_fails_and_restores_when_identity_is_written(tmp_path):
    cfg = _init(tmp_path)
    snapshots = {cfg: _read_bytes_or_none(cfg)}
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True)
    assert b"t@example.com" in _read_bytes_or_none(cfg)
    with pytest.raises(pytest.fail.Exception, match="wrote user"):
        _assert_git_configs_unchanged(snapshots)
    assert b"t@example.com" not in _read_bytes_or_none(cfg)


def test_guard_is_quiet_when_config_is_untouched(tmp_path):
    cfg = _init(tmp_path)
    _assert_git_configs_unchanged({cfg: _read_bytes_or_none(cfg)})


def test_guard_ignores_branch_and_remote_entries(tmp_path):
    cfg = _init(tmp_path)
    snapshots = {cfg: _read_bytes_or_none(cfg)}
    subprocess.run(["git", "-C", str(tmp_path), "config", "branch.x.remote", "origin"], check=True)
    _assert_git_configs_unchanged(snapshots)


def test_guard_teardown_runs_after_monkeypatch_undo(monkeypatch):
    """Teardown spawns git via Popen, so a test's global ``os`` patches must be undone first.

    If the guard tore down before monkeypatch, this test would error at
    teardown with the BlockingIOError raised below.
    """
    import os

    def would_block(*_args, **_kwargs):
        raise BlockingIOError(11, "try again")

    monkeypatch.setattr(os, "read", would_block)
    monkeypatch.setattr(os, "open", would_block)
