"""Move-guard follow-ups (PR #1792 reviews t_01a1d41a33521f17 / t_01a1d2a839617871, card t_01a1d427faa4ce9b). [daveey]

1. every git call of a ``tree_state`` scan draws from its one deadline: the dirty check's hardening probes and
   hidden-edit scan, and every call of the unpushed check (the no-remote-refs trunk path) and the remote refresh;
2. ``state_parts`` knows the ``unchecked worktree`` prefix, so a budget-spent park pin records ``check``/``tree``;
3. a check git could not finish reads ``unchecked worktree <p> (<cause>)``, never ``dirty`` or ``unpushed``;
5. ``card_trees``' other-card owner filter on the ``kanban_continue`` park path (:mod:`hermes_cli.kanban_park_pin`).

Item 4 (the queue repin shares ``check()``'s leaving rule) and the ``kanban_block`` park path are covered by
``test_kanban_move_guard_review_nits``.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from hermes_cli import _subprocess_compat, worktree_archive, worktree_ops
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_move_pool as kmp
from tests.hermes_cli.test_kanban_move_unpushed import _commit, _git, _worktree
from tests.hermes_cli.test_kanban_pool_queue import (
    POOL, _owned_worktree, _placement_events, _pushed_clone, _run, _runs_on, svc, w2)

__all__ = ["svc", "w2"]


class _SlowGit:
    """A fake clock and slow git at every spawn site a ``tree_state`` scan reaches. A call costs ``cost(args)``
    seconds of fake time: with a smaller timeout it spends the timeout and times out, else it runs for real."""

    def __init__(self, monkeypatch, cost):
        self.now, self.cost, self.timeouts = 0.0, cost, []
        monkeypatch.setattr(kmp, "_clock", lambda: self.now)
        real_git, real_run, real_probe = worktree_ops._git, worktree_archive._run, _subprocess_compat.bounded_probe_run

        def git(args, cwd, timeout=10, **kw):
            self._spend(list(args), timeout)
            return real_git(args, cwd, timeout=timeout, **kw)

        def run(args, cwd, env, timeout=300, **kw):  # hidden_index_edits -> worktree_archive._ok
            self._spend(list(args), timeout)
            return real_run(args, cwd, env, timeout=timeout, **kw)

        def probe(argv, *, timeout, **kw):  # noninteractive_repo_git_env's config probes
            try:
                self._spend(list(argv)[1:], timeout)
            except subprocess.TimeoutExpired:
                return None  # bounded_probe_run's answer to a timeout
            return real_probe(argv, timeout=timeout, **kw)

        monkeypatch.setattr(worktree_ops, "_git", git)
        monkeypatch.setattr(worktree_archive, "_run", run)
        monkeypatch.setattr(_subprocess_compat, "bounded_probe_run", probe)

    def _spend(self, args, timeout):
        self.timeouts.append((args[:2], timeout))
        cost = self.cost(args)
        if timeout < cost:
            self.now += timeout
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        self.now += cost


def _clean_pushed_tree(tmp_path, slug):
    tree = _worktree(tmp_path, slug)
    _git(tree, "push", "-q", "origin", f"HEAD:refs/heads/wt/{slug}")
    return tree


# --- 1 + 3: one deadline for every call, and an accurate cause ------------------------------------------------------

def test_a_hung_hidden_edit_scan_spends_the_budget_and_reads_unchecked_not_dirty(tmp_path, monkeypatch):
    """The dirty check's ``ls-files`` (hidden_index_edits, default timeout 120 s) ran outside the deadline."""
    tree = _clean_pushed_tree(tmp_path, "hung-ls-files")
    slow = _SlowGit(monkeypatch, lambda args: 300.0 if args[:1] == ["ls-files"] else 0.5)
    state = kmp.tree_state([str(tree)])
    assert slow.now <= kmp.STATE_BUDGET_S, (slow.now, slow.timeouts)
    assert state == f"unchecked worktree {tree} (git checks of the card's trees took over 20s)", state
    assert kmp.state_parts(state) == ("unchecked", str(tree))


def test_slow_hardening_probes_draw_from_the_deadline(tmp_path, monkeypatch):
    """``noninteractive_repo_git_env`` probed with a fixed 2 s timeout per call, outside the deadline."""
    trees = [_clean_pushed_tree(tmp_path, f"probe-{i}") for i in range(12)]
    slow = _SlowGit(monkeypatch, lambda args: 1.9 if args[:1] in (["-C"], ["config"]) else 0.0)
    state = kmp.tree_state([str(t) for t in trees], budget=5)
    assert slow.now <= 5, (slow.now, slow.timeouts)
    assert state is not None and state.startswith("unchecked worktree "), state


def test_the_no_remote_trunk_path_draws_from_the_deadline_and_a_spent_refresh_reads_unchecked(tmp_path, monkeypatch):
    """No remote-tracking refs: the unpushed check compares with the local trunk (up to 5 git calls), then the
    remote refresh runs out of budget. That used to read ``unpushed commits`` (False from pushed_on_remote)."""
    tree = tmp_path / "code" / "_wt" / "local-only"
    tree.mkdir(parents=True)
    _git(tree, "init", "-q", "-b", "main")
    _commit(tree, "a.txt")
    _git(tree, "checkout", "-q", "-b", "wt/local-only")
    _commit(tree, "b.txt")  # on no remote and not on the local trunk
    _git(tree, "remote", "add", "origin", str(tmp_path / "none.git"))
    slow = _SlowGit(monkeypatch, lambda args: 0.1 if args[:1] in (["-C"], ["config"]) else 3.0)
    state = kmp.tree_state([str(tree)])
    assert slow.now <= kmp.STATE_BUDGET_S, (slow.now, slow.timeouts)
    assert state == f"unchecked worktree {tree} (git checks of the card's trees took over 20s)", state
    assert ["rev-parse", "--verify"] in [a for a, _ in slow.timeouts]  # the trunk path ran under the deadline


def test_a_git_status_timeout_reads_unchecked_not_dirty(tmp_path, monkeypatch):
    tree = _clean_pushed_tree(tmp_path, "status-timeout")
    real = worktree_ops._git

    def git(args, cwd, timeout=10, **kw):
        if args[:1] == ["status"]:
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        return real(args, cwd, timeout=timeout, **kw)

    monkeypatch.setattr(worktree_ops, "_git", git)
    assert kmp.tree_state([str(tree)]) == f"unchecked worktree {tree} (git timed out)"
    assert worktree_ops._worktree_is_dirty(str(tree), str(tree)) is True  # unbudgeted callers keep failing safe


def test_unbudgeted_unpushed_check_still_fails_safe_on_a_timeout(tmp_path, monkeypatch):
    tree = _clean_pushed_tree(tmp_path, "unbudgeted")

    def git(args, cwd, timeout=10, **kw):
        raise subprocess.TimeoutExpired(["git", *args], timeout)

    monkeypatch.setattr(worktree_ops, "_git", git)
    assert worktree_ops._worktree_has_unpushed_commits(str(tree)) is True


def test_an_unreachable_remote_within_budget_still_reads_unpushed(tmp_path):
    tree = _pushed_clone(tmp_path, tmp_path / "gone-remote")
    _commit(tree, "b.txt")
    _git(tree, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert kmp.tree_state([str(tree)]) == f"unpushed commits in {tree}"


# --- 2: state_parts ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("state,parts", [
    ("dirty worktree /w/a", ("dirty", "/w/a")),
    ("unpushed commits in /w/a", ("unpushed", "/w/a")),
    ("unchecked worktree /w/a (git checks of the card's trees took over 20s)", ("unchecked", "/w/a")),
    ("unchecked worktree /w/a (git timed out)", ("unchecked", "/w/a")),
    ("something else", ("", "something else")),
])
def test_state_parts_names_every_check(state, parts):
    assert kmp.state_parts(state) == parts


def test_a_budget_spent_park_pin_records_the_unchecked_check_and_tree(w2, tmp_path, monkeypatch):
    from tools import kanban_tools as kt
    conn_ = w2.conn
    tid = kb.create_task(conn_, title="pool card", assignee="default", runs_on=[POOL])
    tree = _pushed_clone(tmp_path, tmp_path / "code" / "_wt" / "slow-park")
    monkeypatch.setattr(kmp, "tree_state", lambda trees, **kw: (
        f"unchecked worktree {trees[0]} (git checks of the card's trees took over 20s)"))
    _run(w2, monkeypatch, tid)
    assert json.loads(kt._handle_continue({"checkpoint": f"WIP in {tree}"})).get("ok")
    assert _runs_on(conn_, tid) == ["machine:worker-2"]
    ev = _placement_events(conn_, tid)[-1][1]
    assert (ev["check"], ev["tree"]) == ("unchecked", str(tree.resolve())), ev


# --- 5: the other-card owner filter on the park path ----------------------------------------------------------------

def test_a_park_ignores_unpushed_state_in_another_cards_text_named_tree(w2, tmp_path, monkeypatch):
    from tools import kanban_tools as kt
    conn_ = w2.conn
    impl = kb.create_task(conn_, title="implementer", assignee="default", runs_on=[POOL])
    review = kb.create_task(conn_, title="reviewer", assignee="default", runs_on=[POOL])
    main = _pushed_clone(tmp_path, tmp_path / "impl-main")
    tree = _owned_worktree(main, tmp_path / "code" / "_wt" / "impl-tree", impl, "wt/impl-tree")
    _commit(tree, "unpushed.txt")
    _run(w2, monkeypatch, review)  # (UNPUBLISHED_WORK_REASON answers the separate publication guard)
    assert json.loads(kt._handle_continue({"checkpoint": (
        f"Reviewed the implementer's tree {tree}\nUNPUBLISHED_WORK_REASON=the implementer's tree")})).get("ok")
    assert _runs_on(conn_, review) == [POOL] and _placement_events(conn_, review) == []
    _run(w2, monkeypatch, impl)  # the owner's own park still pins on the same tree
    assert json.loads(kt._handle_continue({"checkpoint": f"WIP in {tree}\nUNPUBLISHED_WORK_REASON=wip"})).get("ok")
    assert _runs_on(conn_, impl) == ["machine:worker-2"]
