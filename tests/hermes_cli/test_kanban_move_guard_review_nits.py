"""Move-guard follow-ups from PR #1792's review (5885034090, card t_01a1d2add77deeff). [daveey]

1. A git budget spent inside ``pushed_on_remote``, or a git timeout in the dirty/unpushed checks, reads
   ``unchecked worktree <p>``: neither is evidence of dirty or unpushed work.
2. The worker-first queue repin uses ``check()``'s leaving rule: a repin whose matches include this machine is
   not leaving, so it is not guarded.
3. A park whose text names another card's tree does not pin this card (the ``card_trees`` owner filter).
"""
from __future__ import annotations

import json
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_move as km
from hermes_cli import kanban_move_pool as kmp
from hermes_cli import worktree_ops
from tests.hermes_cli import test_kanban_worker_policy_db as db_fixtures
from tests.hermes_cli.test_kanban_move_unpushed import _commit, _git, _worktree
from tests.hermes_cli.test_kanban_pool_queue import POOL, _placement_events, _run, _runs_on, svc, w2
from tests.hermes_cli.test_kanban_worker_repin import NoCallHub, _acknowledged, _RecordHub

conn = db_fixtures.conn
__all__ = ["conn", "svc", "w2"]


# --- 1: a spent budget or a git timeout reads unchecked ------------------------------------------------------------

def _fake_clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(kmp, "_clock", lambda: now[0])
    return now


def test_a_budget_spent_inside_pushed_on_remote_reads_unchecked(tmp_path, monkeypatch):
    tree = _worktree(tmp_path, "budget-remote")
    _commit(tree, "b.txt")  # unpushed locally: pushed_on_remote asks the remote
    now, real = _fake_clock(monkeypatch), worktree_ops._git_out

    def slow_remote(args, cwd, **kwargs):
        if args[0] == "ls-remote":
            now[0] += 60  # the remote answers after the whole budget is gone
        return real(args, cwd, **kwargs)

    monkeypatch.setattr(worktree_ops, "_git_out", slow_remote)
    state = kmp.tree_state([str(tree)])
    assert state is not None and state.startswith(f"unchecked worktree {tree}"), state


def test_a_remote_timeout_that_spends_the_budget_reads_unchecked(tmp_path, monkeypatch):
    tree = _worktree(tmp_path, "budget-timeout")
    _commit(tree, "b.txt")
    now, real = _fake_clock(monkeypatch), worktree_ops._git_out

    def timing_out(args, cwd, timeout=10, **kwargs):
        if args[0] == "ls-remote":
            now[0] += timeout + 30
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        return real(args, cwd, timeout=timeout, **kwargs)

    monkeypatch.setattr(worktree_ops, "_git_out", timing_out)
    assert kmp.tree_state([str(tree)]).startswith(f"unchecked worktree {tree}")


def test_a_git_timeout_in_the_dirty_check_reads_unchecked_not_dirty(tmp_path, monkeypatch):
    tree = _worktree(tmp_path, "dirty-timeout")
    now, real = _fake_clock(monkeypatch), worktree_ops._git

    def timing_out(args, cwd, timeout=10, **kwargs):
        if args[0] == "status":
            now[0] += timeout
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        return real(args, cwd, timeout=timeout, **kwargs)

    monkeypatch.setattr(worktree_ops, "_git", timing_out)
    assert kmp.tree_state([str(tree)]).startswith(f"unchecked worktree {tree}")


def test_seen_state_within_the_budget_keeps_its_cause(tmp_path):
    tree = _worktree(tmp_path, "seen")
    _commit(tree, "b.txt")
    assert kmp.tree_state([str(tree)]) == f"unpushed commits in {tree}"
    (tree / "wip.txt").write_text("wip\n")
    assert kmp.tree_state([str(tree)]) == f"dirty worktree {tree}"


# --- 2: the queue repin's leaving rule is check()'s ---------------------------------------------------------------

def _registry(monkeypatch, machines):
    from hermes_cli import kanban_worker_node as wn
    monkeypatch.setattr(wn, "registry", lambda **kw: {"machines": machines})


def _unpushed_worker_first_card(conn, tmp_path, monkeypatch, slug):
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    tid = _acknowledged(conn, started_at=1)
    tree = _worktree(tmp_path, slug, owner=tid)
    _commit(tree, "b.txt")
    return tid, tree


def test_a_queue_repin_whose_matches_include_this_machine_is_unguarded(conn, tmp_path, monkeypatch):
    origin = sync.sync_state(conn)["gateway_id"]
    _registry(monkeypatch, {"worker-1": {"labels": [POOL]}, origin: {"labels": [POOL]}})
    tid, _tree = _unpushed_worker_first_card(conn, tmp_path, monkeypatch, "repin-stays")
    assert not km.leaves_here(origin, km.matching_machines([POOL], {origin: {"labels": [POOL]}}, me=origin,
                                                           exclude_me=False))  # check()'s rule says: not leaving
    hub = _RecordHub()
    km.move_queued(conn, tid, runs_on=[POOL], reason="any worker, this one included", hub=hub)
    assert [c["runs_on"] for c in hub.calls] == [[POOL]]  # reached the Board: no unpushed_state_here


def test_a_queue_repin_that_leaves_this_machine_stays_guarded(conn, tmp_path, monkeypatch):
    _registry(monkeypatch, {"worker-1": {"labels": [POOL]}})
    tid, tree = _unpushed_worker_first_card(conn, tmp_path, monkeypatch, "repin-leaves")
    res = km.move_queued(conn, tid, runs_on=[POOL], reason="any worker", hub=NoCallHub())
    assert res["reason_code"] == "unpushed_state_here" and str(tree.resolve()) in res["error"], res


def test_an_unreadable_registry_counts_the_queue_repin_as_leaving(conn, tmp_path, monkeypatch):
    from hermes_cli import kanban_worker_node as wn
    origin = sync.sync_state(conn)["gateway_id"]
    monkeypatch.setattr(wn, "registry", lambda **kw: None)
    assert km._repin_leaves([POOL], origin, None)
    assert not km._repin_leaves([f"machine:{origin}"], origin, None)


# --- 3: park pins follow the card_trees owner filter --------------------------------------------------------------

def _park(kt, text):
    return json.loads(kt._handle_block({"kind": "time", "clock_only": True, "awaiting": {"kind": "clock", "why": "the CI window"}, "reason": (
        f"SCHEDULED_UNTIL=2099-01-01T00:00:00Z {text}")}))


@pytest.mark.parametrize("owner", ["other", "self"])
def test_a_park_naming_a_tree_pins_only_when_the_tree_is_this_cards(w2, tmp_path, monkeypatch, owner):
    from tools import kanban_tools as kt
    conn = w2.conn
    impl = kb.create_task(conn, title="implementer", assignee="default", runs_on=[POOL])
    tid = kb.create_task(conn, title="reviewer", assignee="default", runs_on=[POOL])
    tree = _worktree(tmp_path, f"park-{owner}", owner=impl if owner == "other" else tid)
    _commit(tree, "b.txt")  # unpushed commits in the named tree
    _run(w2, monkeypatch, tid)
    # UNPUBLISHED_WORK_REASON: the separate unpublished-workspace park guard would refuse first; this is the pin path.
    out = _park(kt, f"waiting on CI for the head in {tree}.\nUNPUBLISHED_WORK_REASON=fixture tree")
    assert out.get("ok"), out
    if owner == "other":  # the implementer's state: its own park pins it, not this reviewer's
        assert _runs_on(conn, tid) == [POOL] and _placement_events(conn, tid) == []
    else:
        assert _runs_on(conn, tid) == ["machine:worker-2"]
        assert _placement_events(conn, tid)[-1][1]["unpushed_state"] == f"unpushed commits in {tree.resolve()}"
