"""The conftest guard also restores core.bare, and tests never inherit a caller's GIT_DIR.

2026-10-27: something ran ``git init --bare`` with the live install's repository as its target
(``git init --bare`` with no path and an inherited GIT_DIR does exactly that), the shared .git/config read core.bare=true, and every
worktree and ``hermes update`` failed with "this operation must be run in a work tree".
"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import os
import subprocess
import sys

import pytest

from tests.conftest import _assert_git_configs_unchanged, _read_bytes_or_none


def _init(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path / ".git" / "config"


def _bare(cfg):
    return subprocess.run(["git", "config", "--file", str(cfg), "core.bare"],
                          capture_output=True, text=True, check=False).stdout.strip()


def test_guard_restores_core_bare_and_fails(tmp_path):
    cfg = _init(tmp_path)
    snapshots = {cfg: _read_bytes_or_none(cfg)}
    subprocess.run(["git", "config", "--file", str(cfg), "core.bare", "true"], check=True)

    with pytest.raises(pytest.fail.Exception, match="core.bare into a live checkout"):
        _assert_git_configs_unchanged(snapshots)
    assert _bare(cfg) == "false"


def test_bare_init_through_an_inherited_git_dir_is_caught(tmp_path):
    """The mechanism: ``git init --bare`` with no path and GIT_DIR set reinitializes GIT_DIR as bare
    (``_run(["git", "init", "--bare"], cwd=bare)`` in a test that inherited the caller's GIT_DIR)."""
    live = tmp_path / "live"
    cfg = _init(live)
    snapshots = {cfg: _read_bytes_or_none(cfg)}
    env = {**os.environ, "GIT_DIR": str(live / ".git")}
    store = tmp_path / "store"
    store.mkdir()
    subprocess.run(["git", "init", "-q", "--bare"], check=True, env=env, cwd=store)
    assert _bare(cfg) == "true", "git reinitialized the GIT_DIR repo, not the path"

    with pytest.raises(pytest.fail.Exception, match="core.bare"):
        _assert_git_configs_unchanged(snapshots)
    assert _bare(cfg) == "false"


def test_guard_restores_a_changed_identity_value(tmp_path):
    cfg = _init(tmp_path)
    subprocess.run(["git", "config", "--file", str(cfg), "user.email", "real@example.invalid"], check=True)
    snapshots = {cfg: _read_bytes_or_none(cfg)}
    subprocess.run(["git", "config", "--file", str(cfg), "user.email", "fixture@example.invalid"], check=True)

    with pytest.raises(pytest.fail.Exception, match="wrote user"):
        _assert_git_configs_unchanged(snapshots)
    assert subprocess.run(["git", "config", "--file", str(cfg), "user.email"],
                          capture_output=True, text=True, check=False).stdout.strip() == "real@example.invalid"


@pytest.mark.skipif(os.environ.get("HERMES_GIT_SCRUB_PROBE") != "1", reason="probe for the test below")
def test_probe_sees_no_inherited_git_location():
    assert not {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"} & set(os.environ)


def test_tests_do_not_inherit_the_callers_git_dir(tmp_path):
    repo = tmp_path / "caller"
    _init(repo)
    env = {**os.environ, "HERMES_GIT_SCRUB_PROBE": "1", "GIT_DIR": str(repo / ".git"),
           "GIT_WORK_TREE": str(repo), "GIT_INDEX_FILE": str(repo / ".git" / "index")}
    env.pop("PYTEST_XDIST_WORKER", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly",
         f"{__file__}::test_probe_sees_no_inherited_git_location"],
        capture_output=True, text=True, env=env, cwd=os.path.dirname(os.path.dirname(__file__)), timeout=300,
        check=False,
    )
    assert "1 passed" in result.stdout, result.stdout[-2000:] + result.stderr[-2000:]
