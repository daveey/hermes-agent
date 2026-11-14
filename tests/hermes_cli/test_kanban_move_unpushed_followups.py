"""Follow-ups to the unpushed-state move guard (PR #1681 review t_01a1d113d42a1b13, card t_01a1d1197fa00599). [daveey]

* the move's own checkpoint is scanned for tree paths (check() runs before it is written);
* a tree the text names but whose owner marker names another card is that card's work (a reviewer naming the
  implementer's ``~/code/_wt/<slug>``);
* git time for one card's trees is bounded (operator call scan, pickup-stall alarm);
* a worker-first card's Board repin to the worker queue takes the guard too (it skipped ``check()``).
"""
from __future__ import annotations

import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_move as km
from hermes_cli import kanban_move_pool as kmp
from hermes_cli import worktree_ops
from tests.hermes_cli import test_kanban_worker_policy_db as db_fixtures
from tests.hermes_cli.test_kanban_move_unpushed import (
    TARGET, _card, _commit, _git, _moved, _refused, _worktree, hub)
from tests.hermes_cli.test_kanban_worker_repin import NoCallHub, _acknowledged, _RecordHub

# kanban_move_pool: a machine:<x> target needs a host_reason naming state on x (t_01a1d95c41620e6c).
HR = "the card's worktree is on that host"

conn = db_fixtures.conn
__all__ = ["conn", "hub"]


# --- 1: the move's own checkpoint ----------------------------------------------------------------------------------

def test_a_tree_named_only_in_the_queued_moves_checkpoint_keeps_the_card_here(hub, tmp_path):
    tid = _card(hub)
    tree = _worktree(tmp_path, "ckpt-queued")  # no owner marker, nothing stored names it
    _commit(tree, "b.txt")
    res = km.move_queued(hub.conn, tid, runs_on=TARGET, host_reason=HR, reason="slots", checkpoint=f"WIP in {tree}, not pushed")
    _refused(res, tree)
    assert _moved(hub.conn, tid) == []


def test_a_tree_named_only_in_the_running_moves_checkpoint_keeps_the_card_here(hub, tmp_path):
    from tests.hermes_cli.test_kanban_worker_node import HUB
    tid = _card(hub, runs_on=[f"machine:{HUB}"])
    hub.round()
    hub.tick()
    run_id = hub.conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    tree = _worktree(tmp_path, "ckpt-running")
    _commit(tree, "b.txt")
    res = km.move_task(hub.conn, tid, runs_on=TARGET, host_reason=HR, reason="r", checkpoint=f"Done: commit b in {tree}.",
                       expected_run_id=run_id)
    _refused(res, tree)
    assert kb.get_task(hub.conn, tid).status == "running"


# --- 3: another card's text-named tree ----------------------------------------------------------------------------

def test_a_text_named_tree_owned_by_another_card_does_not_keep_this_card(hub, tmp_path):
    impl = _card(hub, title="implementer")
    tree = _worktree(tmp_path, "impl-owned", owner=impl)
    _commit(tree, "b.txt")
    review = _card(hub, title="reviewer", body=f"Review the implementer's tree at {tree} (head b).")
    assert km.move_queued(hub.conn, review, runs_on=TARGET, host_reason=HR, reason="slots")["ok"]
    _refused(km.move_queued(hub.conn, impl, runs_on=TARGET, host_reason=HR, reason="slots"), tree)  # still its owner's state


def test_a_text_named_tree_owned_by_the_card_itself_still_counts(hub, tmp_path):
    tid = _card(hub)
    tree = _worktree(tmp_path, "self-owned", owner=tid)
    _commit(tree, "b.txt")
    assert kmp.card_trees(tid, None, f"see {tree}", linked_text_only=True) == [str(tree.resolve())]


# --- 4: bounded git time ----------------------------------------------------------------------------------------

def test_git_time_for_many_slow_trees_is_bounded_and_fails_safe(hub, tmp_path, monkeypatch):
    tid = _card(hub)
    first = _worktree(tmp_path, "slow-0", owner=tid)
    main = tmp_path / "slow-0-main"
    trees = [first]
    for i in range(1, 20):  # clean trees whose commits are on the remote: each one costs every check
        tree = tmp_path / "code" / "_wt" / f"slow-{i}"
        _git(main, "worktree", "add", "-q", "-b", f"wt/slow-{i}", str(tree), "origin/main")
        trees.append(tree)
    for tree in trees:
        _git(tree, "push", "-q", "origin", f"HEAD:refs/heads/{tree.name}")
    now, calls, real = [0.0], [], worktree_ops._git

    def slow_git(args, cwd, timeout=10, **kwargs):  # every git call takes 4 s of fake time
        calls.append(timeout)
        if timeout < 4:
            now[0] += timeout
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        now[0] += 4
        return real(args, cwd, timeout=timeout, **kwargs)

    monkeypatch.setattr(kmp, "_clock", lambda: now[0], raising=False)  # raising=False: RED on the unbounded scan
    monkeypatch.setattr(worktree_ops, "_git", slow_git)
    state = kmp.tree_state([str(t) for t in trees])
    assert now[0] <= getattr(kmp, "STATE_BUDGET_S", 20.0), (now[0], len(calls))
    assert state is not None, "a budget-spent scan fails safe (the unchecked trees count as state)"
    assert all(t <= 10 for t in calls)


def test_a_fast_scan_within_the_budget_reads_clean(hub, tmp_path):
    tid = _card(hub)
    tree = _worktree(tmp_path, "fast", owner=tid)
    _git(tree, "push", "-q", "origin", "HEAD:refs/heads/wt/fast")
    assert kmp.tree_state([str(tree)]) is None


# --- 6: the worker-first Board repin to the queue -------------------------------------------------------------------

@pytest.fixture
def pool_registry(monkeypatch):
    """A live Board registry with one unheld pool worker (pool repin eligibility reads it)."""
    from hermes_cli import kanban_worker_node as wn
    monkeypatch.setattr(wn, "registry", lambda **kw: {"machines": {"worker-1": {"labels": ["pool:worker"]}}})


def test_a_started_worker_first_cards_queue_repin_is_refused_on_unpushed_state(conn, tmp_path, monkeypatch, pool_registry):
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    tid = _acknowledged(conn, started_at=1)
    tree = _worktree(tmp_path, "repin", owner=tid)
    _commit(tree, "b.txt")
    res = km.move_queued(conn, tid, runs_on=["pool:worker"], reason="any worker", hub=NoCallHub())
    assert res["reason_code"] == "unpushed_state_here" and str(tree.resolve()) in res["error"], res
    hub = _RecordHub()
    km.move_queued(conn, tid, runs_on=["pool:worker"], reason="abandon it", hub=hub, force=True)
    assert [c["runs_on"] for c in hub.calls] == [["pool:worker"]]  # force reaches the Board


def test_a_clean_worker_first_cards_queue_repin_reaches_the_board(conn, tmp_path, monkeypatch, pool_registry):
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    tid = _acknowledged(conn, started_at=1)
    tree = _worktree(tmp_path, "repin-clean", owner=tid)
    _git(tree, "push", "-q", "origin", "HEAD:refs/heads/wt/repin-clean")
    hub = _RecordHub()
    km.move_queued(conn, tid, runs_on=["pool:worker"], reason="any worker", hub=hub)
    assert [c["runs_on"] for c in hub.calls] == [["pool:worker"]]
