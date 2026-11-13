"""Scheduled scratch GC finishes inside its cap: refusal backoff and a pass budget. [daveey]

worker-2 (2026-11-13): the default board's ``hermes kanban gc`` re-archived the same
refused trees every hour (one partial clone cost ~78 s per pass) and ran past the
1800 s wrapper kill, so its ``GC complete`` line never printed and the pass failed.
"""
import argparse
import json
import logging

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_ops
from hermes_cli import kanban_scratch_archive as sa
from hermes_cli import kanban_scratch_backoff as bo
from hermes_cli import kanban_worktree_gc as gc
from tests.hermes_cli import test_kanban_scratch_gc_archive as helpers
from tests.hermes_cli.test_kanban_scratch_gc_archive import OLD, _run, _task

board = helpers.board
remote = helpers.remote

pytestmark = pytest.mark.usefixtures("board")


@pytest.fixture
def archives(monkeypatch):
    """Record every archive attempt (path) while running the real archiver."""
    calls = []
    real = sa.archive_workspace

    def spy(workspace, *args, **kwargs):
        calls.append(workspace)
        return real(workspace, *args, **kwargs)

    monkeypatch.setattr(sa, "archive_workspace", spy)
    return calls


def _bare_refused(remote):
    tid, path = _task(remote=remote)
    _run("clone", "-q", "--bare", str(remote), str(path / "mirror.git"))
    return tid, path


def _state():
    return json.loads(bo._path("default").read_text())["entries"]


def _expire(key):
    data = json.loads(bo._path("default").read_text())
    data["entries"][key]["retry_at"] = 0
    bo._path("default").write_text(json.dumps(data))


def test_refused_tree_is_not_rearchived_until_backoff_passes(remote, archives, caplog):
    tid, path = _bare_refused(remote)
    assert helpers._gc() == 0
    assert archives == [path]
    entry = _state()[tid]
    assert entry["count"] == 1 and "bare repository" in entry["reason"]
    assert entry["retry_at"] - entry["last_at"] == bo.BASE_S

    caplog.set_level(logging.INFO, logger="hermes_cli.kanban_scratch_backoff")
    assert helpers._gc() == 0
    assert archives == [path], "a refusal inside its backoff window must not be re-archived"
    assert path.exists() and "archive refused 1 time(s)" in caplog.text

    _expire(tid)
    assert helpers._gc() == 0
    assert archives == [path, path]
    entry = _state()[tid]
    assert entry["count"] == 2 and entry["retry_at"] - entry["last_at"] == 2 * bo.BASE_S
    assert path.exists()


def test_changed_tree_is_retried_at_once_and_success_clears_the_entry(remote, archives):
    tid, path = _bare_refused(remote)
    assert helpers._gc() == 0
    import shutil
    shutil.rmtree(path / "mirror.git")  # the root's mtime moves: a new fingerprint
    assert helpers._gc() == 0
    assert archives == [path, path]
    assert not path.exists()
    assert tid not in _state()


def test_backoff_doubles_to_a_cap(tmp_path):
    clock = [1_000_000.0]
    refusals = bo.Refusals("default", now=lambda: clock[0])
    tree = tmp_path / "tree"
    tree.mkdir()
    waits = []
    for _ in range(7):
        refusals.record("t", tree, 5, "refused")
        entry = refusals.entries["t"]
        waits.append(entry["retry_at"] - entry["last_at"])
        assert refusals.backing_off("t", tree, 5) is not None
        assert refusals.backing_off("t", tree, 6) is None, "a new completed_at is a new tree"
        clock[0] = entry["retry_at"]
        assert refusals.backing_off("t", tree, 5) is None
    assert waits == [bo.BASE_S * 2 ** i for i in range(4)] + [bo.CAP_S] * 3
    clock[0] += bo.FORGET_S + 1
    assert bo.Refusals("default", now=lambda: clock[0]).entries == {}


def test_unreadable_state_is_empty_and_never_blocks(remote, archives):
    bo._path("default").parent.mkdir(parents=True, exist_ok=True)
    bo._path("default").write_text("{not json")
    tid, path = _bare_refused(remote)
    assert helpers._gc() == 0
    assert archives == [path] and _state()[tid]["count"] == 1


def test_budget_defers_the_rest_oldest_first_and_pass_completes(remote, archives, caplog, capsys):
    _newer, newer_path = _task(remote=remote)
    _older, older_path = _task(remote=remote, completed_at=OLD - 3600)
    ticks = iter([0.0, 0.0, 5.0])  # started, first check (in budget), second check (spent)
    budget = bo.Budget(1, clock=lambda: next(ticks, 9.0))
    with kbc.connect_closing() as conn:
        assert gc.gc_task_scratch(conn, older_than_days=7, budget=budget) == 1
    assert archives == [older_path]
    assert not older_path.exists() and newer_path.exists()
    assert budget.deferred == 1 and "deferred 1 aged scratch tree(s)" in caplog.text
    # Next pass (fresh budget) picks the deferred tree up.
    assert helpers._gc() == 0
    assert not newer_path.exists()
    assert "GC complete: 1 workspace(s)" in capsys.readouterr().out


def _cli(*argv):
    from hermes_cli.kanban_parser import build_parser
    root = argparse.ArgumentParser()
    build_parser(root.add_subparsers(dest="command"))
    return root.parse_args(["kanban", "gc", *argv])


def test_cli_budget_flag_and_default(monkeypatch):
    assert _cli().scratch_budget_seconds is None
    assert _cli("--scratch-budget-seconds", "0").scratch_budget_seconds == 0
    seen = []
    monkeypatch.setattr(gc, "gc_task_scratch", lambda conn, **kw: seen.append(kw["budget"]) or 0)
    for value in (None, 0, 30):
        kanban_ops._cmd_gc(argparse.Namespace(event_retention_days=0, log_retention_days=0,
                                              worktree_retention_days=7, scratch_budget_seconds=value))
    default, unlimited, short = seen
    assert default.deadline - default.started == bo.DEFAULT_BUDGET_S
    assert unlimited.deadline is None and not unlimited.exhausted()
    assert short.deadline - short.started == 30


def test_default_budget_leaves_headroom_below_the_wrapper_kill():
    assert bo.DEFAULT_BUDGET_S <= 1800 - 600
