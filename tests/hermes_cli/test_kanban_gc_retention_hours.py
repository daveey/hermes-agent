"""``kanban.worktree_retention_hours``: an optional sub-day GC window that
overrides ``worktree_retention_days`` when set and leaves it alone when unset.
An invalid hours value disables age-based GC (it never falls back to days)."""
import argparse
import logging
import math
import time

import pytest

from hermes_cli import kanban_ops
from hermes_cli import kanban_worktree_gc as gc
from tests.hermes_cli import test_kanban_scratch_gc_archive as helpers

board = helpers.board
remote = helpers.remote


def _config(text: str) -> None:
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _gc_from_config():
    # worktree_retention_days=None: the window comes from retention_days(), as on the hourly worker pass.
    return kanban_ops._cmd_gc(argparse.Namespace(
        event_retention_days=0, log_retention_days=0, worktree_retention_days=None,
    ))


@pytest.mark.parametrize(("text", "expected"), [
    pytest.param("kanban:\n  worktree_retention_days: 2\n  worktree_retention_hours: 3\n", 3 / 24,
                 id="hours-override-days"),
    pytest.param("kanban:\n  worktree_retention_days: 0\n  worktree_retention_hours: 6\n", 6 / 24,
                 id="hours-override-disabled-days"),
    pytest.param("kanban:\n  worktree_retention_days: 2\n  worktree_retention_hours: 1.5\n", 1.5 / 24,
                 id="fractional-hours"),
    pytest.param("kanban:\n  worktree_retention_days: 2\n  worktree_retention_hours: 0\n", 0,
                 id="zero-hours-disables"),
    pytest.param("kanban:\n  worktree_retention_days: 2\n", 2, id="unset-keeps-days"),
    pytest.param("kanban:\n  worktree_retention_days: 2\n  worktree_retention_hours:\n", 2,
                 id="null-is-unset"),
])
def test_hours_precedence(board, text, expected):
    _config(text)
    assert gc.retention_days() == expected


def test_unset_hours_keeps_days_type_exactly(board):
    _config("kanban:\n  worktree_retention_days: 1\n")
    value = gc.retention_days()
    assert value == 1 and type(value) is int


def test_seeded_default_is_unset_and_keeps_days_behaviour(board):
    """DEFAULT_CONFIG seeds hours as None (so `hermes config set` knows the key); with no user
    value the merged config still yields the plain 7-day window."""
    from hermes_cli.config import _validate_config_key
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["kanban"]["worktree_retention_hours"] is None
    assert _validate_config_key("kanban.worktree_retention_hours") == (True, None)
    _config("kanban: {}\n")
    value = gc.retention_days()
    assert value == DEFAULT_CONFIG["kanban"]["worktree_retention_days"] == 7 and type(value) is int


@pytest.mark.parametrize("raw", ["-1", "-0.5", "'3'", "true", "false", ".nan", ".inf", "[3]", "{h: 3}"])
def test_invalid_hours_disables_and_warns(board, caplog, raw):
    # A valid days key must not mask a bad hours key: the operator asked for hours.
    _config(f"kanban:\n  worktree_retention_days: 2\n  worktree_retention_hours: {raw}\n")
    caplog.set_level(logging.WARNING, logger=gc.log.name)
    assert gc.retention_days() == 0
    assert "age-based GC disabled" in caplog.text


def test_hours_cutoff_archives_old_tree_and_keeps_young_one(board, remote):
    _config("kanban:\n  worktree_retention_days: 7\n  worktree_retention_hours: 3\n")
    now = int(time.time())
    old_tid, old_path = helpers._task(remote=remote, completed_at=now - 4 * 3600)
    young_tid, young_path = helpers._task(remote=remote, completed_at=now - 2 * 3600)
    assert _gc_from_config() == 0
    # Past 3 h: archived first, then removed (the 7-day key alone would have kept it).
    assert not old_path.exists()
    assert (helpers._archive(old_tid) / "repo" / "meta.json").is_file()
    assert any(kind == "workspace_archived" for kind, _ in helpers._events(old_tid))
    # Inside 3 h: untouched.
    assert (young_path / "repo" / ".git").is_dir()
    assert not helpers._archive(young_tid).exists()


def test_hours_cutoff_reports_pending_deadline(board, remote, caplog):
    _config("kanban:\n  worktree_retention_hours: 3\n")
    finished = int(time.time()) - 3600
    _, path = helpers._task(remote=remote, completed_at=finished)
    caplog.set_level(logging.WARNING)
    assert _gc_from_config() == 0
    assert path.exists()
    line = next(r for r in caplog.text.splitlines() if f"path={path}" in r and "retention_pending" in r)
    eligible = float(line.split("eligible_at=")[1].split(";")[0])
    assert math.isclose(eligible, finished + 3 * 3600)


def test_unset_hours_keeps_day_cutoff(board, remote):
    _config("kanban:\n  worktree_retention_days: 1\n")
    tid, path = helpers._task(remote=remote, completed_at=int(time.time()) - 4 * 3600)
    assert _gc_from_config() == 0
    assert (path / "repo" / ".git").is_dir()
    assert not helpers._archive(tid).exists()


def test_invalid_hours_keeps_everything(board, remote):
    _config("kanban:\n  worktree_retention_days: 1\n  worktree_retention_hours: -3\n")
    tid, path = helpers._task(remote=remote)  # finished 10 days ago: due under the days key
    assert _gc_from_config() == 0
    assert (path / "repo" / ".git").is_dir()
    assert not helpers._archive(tid).exists()


def test_explicit_cli_days_still_wins(board, remote):
    _config("kanban:\n  worktree_retention_hours: 3\n")
    tid, path = helpers._task(remote=remote, completed_at=int(time.time()) - 4 * 3600)
    assert helpers._gc(days=1) == 0
    assert (path / "repo" / ".git").is_dir()
    assert not helpers._archive(tid).exists()
