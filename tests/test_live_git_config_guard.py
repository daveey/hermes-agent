"""Regression: the conftest guard catches writes to a checkout's shared .git/config."""

import subprocess

import pytest

from tests.conftest import _assert_git_configs_unchanged, _live_git_config_paths, _read_bytes_or_none


def _init(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path / ".git" / "config"


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
