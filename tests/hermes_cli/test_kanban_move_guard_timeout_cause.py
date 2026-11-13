"""Move-guard cause text from PR #2111's review (5889959817, card t_01a1d49da268115f). [daveey]

``tree_state`` read a check as timed out when the WHOLE check took at least the timeout of ONE git call. The
unpushed check makes 2-3 git calls, so a slow tree whose calls each finished in time read ``unchecked (git timed
out)`` instead of ``unpushed commits in``. Since e9d537b75a the checks report a git timeout explicitly (a budgeted
check raises ``GitUnchecked``); these tests pin that cause text for the multi-call cases.
"""
from __future__ import annotations

import subprocess

import pytest

from hermes_cli import kanban_move_pool as kmp
from hermes_cli import worktree_ops
from hermes_cli.git_budget import GitUnchecked, deadline_budget
from tests.hermes_cli.test_kanban_move_unpushed import _commit, _worktree

_UNPUSHED_CALLS = (["for-each-ref", "--format=%(refname)", "refs/remotes"], ["log"])


def _is_unpushed_check_call(args) -> bool:
    return list(args) == _UNPUSHED_CALLS[0] or args[0] == "log"


def _fake_clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(kmp, "_clock", lambda: now[0])
    return now


def test_a_slow_multi_call_check_that_never_times_out_keeps_its_unpushed_cause(tmp_path, monkeypatch):
    tree = _worktree(tmp_path, "slow-unpushed")
    _commit(tree, "b.txt")
    now, real = _fake_clock(monkeypatch), worktree_ops._git_out
    seen: list[tuple[float, float]] = []

    def slow(args, cwd, timeout=10, **kwargs):
        if _is_unpushed_check_call(args):
            step = timeout * 0.6  # each call ends well inside its own timeout; together they pass one timeout
            seen.append((step, timeout))
            now[0] += step
        return real(args, cwd, timeout=timeout, **kwargs)

    monkeypatch.setattr(worktree_ops, "_git_out", slow)
    assert kmp.tree_state([str(tree)]) == f"unpushed commits in {tree}"
    first = seen[:2]  # the unpushed check's own two calls (pushed_on_remote makes neither)
    assert len(first) == 2 and sum(s for s, _ in first) >= first[0][1]  # the old whole-check comparison tripped


def test_a_git_timeout_in_the_unpushed_check_reads_unchecked_however_fast(tmp_path, monkeypatch):
    tree = _worktree(tmp_path, "unpushed-timeout")
    _commit(tree, "b.txt")
    _fake_clock(monkeypatch)  # the clock never moves: only the explicit report says it timed out
    real = worktree_ops._git_out

    def timing_out(args, cwd, timeout=10, **kwargs):
        if args[0] == "log":
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        return real(args, cwd, timeout=timeout, **kwargs)

    monkeypatch.setattr(worktree_ops, "_git_out", timing_out)
    state = kmp.tree_state([str(tree)])
    assert state is not None and state.startswith(f"unchecked worktree {tree} (git timed out"), state


def test_a_git_timeout_in_the_dirty_checks_hidden_index_read_reads_unchecked(tmp_path, monkeypatch):
    tree = _worktree(tmp_path, "hidden-timeout")
    _fake_clock(monkeypatch)
    from hermes_cli import worktree_retention

    def timing_out(_path, *args, **kwargs):
        raise subprocess.TimeoutExpired(["git", "ls-files"], 60)

    monkeypatch.setattr(worktree_retention, "hidden_index_edits", timing_out)
    state = kmp.tree_state([str(tree)])
    assert state is not None and state.startswith(f"unchecked worktree {tree} (git timed out"), state


def test_the_checks_report_timeouts_only_to_a_budgeted_caller(tmp_path, monkeypatch):
    tree = _worktree(tmp_path, "report")

    def timing_out(args, cwd, timeout=10, **kwargs):
        raise subprocess.TimeoutExpired(["git", *args], timeout)

    monkeypatch.setattr(worktree_ops, "_git_out", timing_out)
    monkeypatch.setattr(worktree_ops, "_git", timing_out)
    assert worktree_ops._worktree_has_unpushed_commits(str(tree)) is True  # fail safe, unchanged
    assert worktree_ops._worktree_is_dirty(str(tree), str(tree)) is True
    with pytest.raises(GitUnchecked, match="git timed out"):
        worktree_ops._worktree_has_unpushed_commits(str(tree), budget=deadline_budget(60))
    with pytest.raises(GitUnchecked, match="git timed out"):
        worktree_ops._worktree_is_dirty(str(tree), str(tree), budget=deadline_budget(60))
