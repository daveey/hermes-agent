"""A commit-less repo (``git init``, nothing committed) among a card's trees holds no state to carry. [daveey]

11-13: pooled card t_01a1d5e41b24e463 parked on worker-6 with a scratch fixture ``core-fixture`` (``git init``, no
commits, no remotes, clean) in its workspace. ``tree_state`` called it "unpushed commits" (the unpushed check's
fail-safe treats ``git log HEAD`` on an unborn HEAD as a git error) and the park pinned the card to worker-6,
though no other worker would miss anything. A commit-less tree with files in it is still dirty, and a tree whose
branch ref names a missing object is still state.
"""
from __future__ import annotations

import json
import subprocess

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_move_pool as kmp
from hermes_cli import kanban_park_pin as kpp
from tests.hermes_cli import test_kanban_pool_queue as pq

svc = pq.svc
w2 = pq.w2
POOL = pq.POOL


def _init(tree):
    tree.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(tree)], check=True, capture_output=True)
    return tree


def test_an_empty_git_init_tree_has_no_state(tmp_path):
    tree = _init(tmp_path / "core-fixture")
    assert kmp.tree_state([str(tree)]) is None


def test_a_commit_less_tree_with_an_untracked_file_is_dirty(tmp_path):
    tree = _init(tmp_path / "core-fixture")
    (tree / "notes.txt").write_text("work\n")
    assert kmp.tree_state([str(tree)]) == f"dirty worktree {tree}"


def test_a_commit_less_tree_with_a_staged_file_is_dirty(tmp_path):
    tree = _init(tmp_path / "core-fixture")
    (tree / "fix.py").write_text("x = 1\n")
    pq._git(tree, "add", "fix.py")
    assert kmp.tree_state([str(tree)]) == f"dirty worktree {tree}"


def test_a_branch_ref_holding_a_missing_object_is_still_state(tmp_path):
    tree = _init(tmp_path / "core-fixture")
    branch = subprocess.run(["git", "symbolic-ref", "HEAD"], cwd=tree, check=True, capture_output=True,
                            text=True).stdout.strip()
    ref = tree / ".git" / branch
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text("1" * 40 + "\n")
    assert kmp.tree_state([str(tree)]) is not None


def test_a_detached_commit_no_ref_holds_is_still_unpushed(tmp_path):
    """No refs at all, but HEAD names a commit: that commit is work another worker would miss."""
    tree = _init(tmp_path / "core-fixture")
    pq._commit(tree, "a.txt")
    pq._git(tree, "checkout", "-q", "--detach")
    branch = subprocess.run(["git", "for-each-ref", "--format=%(refname:short)"], cwd=tree, check=True,
                            capture_output=True, text=True).stdout.split()
    pq._git(tree, "branch", "-qD", *branch)
    assert kmp.tree_state([str(tree)]) == f"unpushed commits in {tree}"


def test_an_unborn_branch_beside_an_unpushed_branch_is_still_unpushed(tmp_path):
    """HEAD is unborn (an orphan branch), but another local branch holds a commit no remote has."""
    tree = _init(tmp_path / "core-fixture")
    pq._commit(tree, "a.txt")
    pq._git(tree, "switch", "-q", "--orphan", "fresh")
    assert kmp.tree_state([str(tree)]) == f"unpushed commits in {tree}"


def test_a_failing_ref_listing_keeps_the_fail_safe(tmp_path, monkeypatch):
    """``for-each-ref`` exiting non-zero with no output is no proof of an empty repo."""
    from hermes_cli import worktree_ops
    real = worktree_ops._git

    def git(args, cwd, *a, **kw):
        if args and args[0] == "for-each-ref" and "refs/remotes" not in args:
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: broken\n")
        return real(args, cwd, *a, **kw)
    monkeypatch.setattr(worktree_ops, "_git", git)
    tree = _init(tmp_path / "core-fixture")
    assert kmp.tree_state([str(tree)]) == f"unpushed commits in {tree}"


def test_a_ref_listing_that_fails_silently_keeps_the_fail_safe(tmp_path, monkeypatch):
    """A ``for-each-ref`` killed by a signal (exit -9, no stdout and no stderr) is no proof of an empty repo: the
    exit code alone must keep the fail-safe, since the stderr check has nothing to see."""
    from hermes_cli import worktree_ops
    real = worktree_ops._git

    def git(args, cwd, *a, **kw):
        if args and args[0] == "for-each-ref" and "refs/remotes" not in args:
            return subprocess.CompletedProcess(["git", *args], -9, "", "")
        return real(args, cwd, *a, **kw)
    monkeypatch.setattr(worktree_ops, "_git", git)
    tree = _init(tmp_path / "core-fixture")
    assert kmp.tree_state([str(tree)]) == f"unpushed commits in {tree}"


def _commit_behind_a_broken_ref(tmp_path, ref_text):
    """A clean repo holding commit c1 whose only ref is broken: index and file gone, so status reads clean, HEAD
    does not resolve, and ``for-each-ref`` skips the ref with a stderr warning, exit 0 and no output."""
    tree = _init(tmp_path / "core-fixture")
    pq._commit(tree, "f")
    (tree / ".git" / "index").unlink()
    (tree / "f").unlink()
    branch = subprocess.run(["git", "symbolic-ref", "HEAD"], cwd=tree, check=True, capture_output=True,
                            text=True).stdout.strip()
    (tree / ".git" / branch).write_text(ref_text)
    return tree


def test_a_clean_repo_whose_only_ref_is_garbage_keeps_the_fail_safe(tmp_path):
    """11-13 hub repro: c1 is in the object store, so the tree is not commit-less."""
    tree = _commit_behind_a_broken_ref(tmp_path, "garbage-not-a-sha\n")
    assert kmp.tree_state([str(tree)]) == f"unpushed commits in {tree}"


def test_a_clean_repo_whose_only_ref_file_is_empty_keeps_the_fail_safe(tmp_path):
    """A zero-length ref file (a crash mid-write): git 2.43/2.55 skip it as broken just like garbage."""
    tree = _commit_behind_a_broken_ref(tmp_path, "")
    assert kmp.tree_state([str(tree)]) == f"unpushed commits in {tree}"


def test_a_head_probe_git_error_keeps_the_fail_safe_even_with_no_refs_listed(tmp_path, monkeypatch):
    """Only rev-parse exit 1 means unborn: exit 128 (a git error) is no proof, however empty the ref list."""
    from hermes_cli import worktree_ops
    real = worktree_ops._git

    def git(args, cwd, *a, **kw):
        if args[:2] == ["rev-parse", "--verify"]:
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: broken\n")
        if args and args[0] == "for-each-ref" and "refs/remotes" not in args:
            return subprocess.CompletedProcess(["git", *args], 0, "", "")
        return real(args, cwd, *a, **kw)
    monkeypatch.setattr(worktree_ops, "_git", git)
    tree = _init(tmp_path / "core-fixture")
    assert kmp.tree_state([str(tree)]) == f"unpushed commits in {tree}"


def test_a_head_probe_that_times_out_keeps_the_fail_safe(tmp_path, monkeypatch):
    from hermes_cli import worktree_ops
    real = worktree_ops._git

    def git(args, cwd, *a, **kw):
        if args[:2] == ["rev-parse", "--verify"]:
            raise subprocess.TimeoutExpired(["git", *args], 10)
        return real(args, cwd, *a, **kw)
    monkeypatch.setattr(worktree_ops, "_git", git)
    tree = _init(tmp_path / "core-fixture")
    assert kmp.tree_state([str(tree)]) is not None


def _card_with_fixture(w2, tmp_path, monkeypatch, *, runs_on):
    conn = w2.conn
    tid = kb.create_task(conn, title="pool card with a scratch fixture", assignee="default", runs_on=[POOL])
    ws = tmp_path / "ws"
    ws.mkdir()
    fixture = _init(ws / "core-fixture")
    repo = pq._pushed_clone(tmp_path, ws / "repo")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_path = ?, runs_on = ? WHERE id = ?",
                     (str(ws), json.dumps(runs_on), tid))
    return conn, tid, fixture, repo


def test_park_keeps_a_card_whose_only_unpushed_looking_tree_is_commit_less_on_the_pool(w2, tmp_path, monkeypatch):
    conn, tid, _fixture, _repo = _card_with_fixture(w2, tmp_path, monkeypatch, runs_on=[POOL])
    pq._run(w2, monkeypatch, tid)
    assert kpp.plan(conn, tid, "waiting on CI") is None


def test_a_card_pinned_by_a_commit_less_tree_is_unpinned_at_the_next_park(w2, tmp_path, monkeypatch):
    conn, tid, fixture, repo = _card_with_fixture(w2, tmp_path, monkeypatch, runs_on=["machine:worker-2"])
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "pool_pinned", {
            "from_machine": "worker-2", "runs_on": ["machine:worker-2"], "previous_runs_on": [POOL],
            "unpushed_state": f"unpushed commits in {fixture.resolve()}",
            "trees": [str(fixture.resolve()), str(repo.resolve())]})
    pq._run(w2, monkeypatch, tid)
    step = kpp.plan(conn, tid, "waiting on CI")
    assert step is not None and step["op"] == "unpin" and step["restore"] == [POOL], step
