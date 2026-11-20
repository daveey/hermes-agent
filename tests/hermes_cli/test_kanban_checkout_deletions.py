"""Published checkout-only deletions hold no card state; mixed/unknown state still pins."""
from __future__ import annotations

import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_move_pool as kmp
from hermes_cli import kanban_park_pin as kpp
from tests.hermes_cli import test_kanban_pool_queue as pq

svc = pq.svc
w2 = pq.w2


def _checkout(tmp_path, *, empty_index=False):
    ws = tmp_path / "ws"
    ws.mkdir()
    repo = pq._pushed_clone(tmp_path, ws / "repo")
    pq._commit(repo, "b.txt")
    pq._commit(repo, "hidden.txt")
    pq._git(repo, "push", "-qu", "origin", "HEAD:wt/x")
    pq._git(repo, "worktree", "add", "-qb", "pr", str(ws / "polyworld"))
    pq._git(ws / "polyworld", "push", "-qu", "origin", "HEAD:pr")
    if empty_index:
        pq._git(repo, "rm", "-q", "a.txt", "b.txt", "hidden.txt")
    else:
        (repo / "a.txt").unlink()
    return ws, repo


@pytest.mark.parametrize("empty_index", [False, True])
def test_published_checkout_deletions_do_not_pin(w2, tmp_path, monkeypatch, empty_index):
    ws, repo = _checkout(tmp_path, empty_index=empty_index)
    assert set(kmp.card_trees(None, str(ws))) == {str(repo), str(ws / "polyworld")}
    tid = kb.create_task(w2.conn, title="published PR with owning clone", assignee="default", runs_on=[pq.POOL])
    with kb.write_txn(w2.conn):
        w2.conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(ws), tid))
    pq._run(w2, monkeypatch, tid)
    assert kpp.plan(w2.conn, tid, "waiting on CI") is None


@pytest.mark.parametrize("change", ["modified", "untracked", "unpushed", "staged", "hidden-assume", "hidden-skip",
                                   "no-upstream", "detached", "mixed", "empty-unpushed"])
def test_other_checkout_state_still_pins(w2, tmp_path, monkeypatch, change):
    ws, repo = _checkout(tmp_path)
    if change == "modified":
        (repo / "b.txt").write_text("work\n")
    elif change == "untracked":
        (repo / "new.txt").write_text("work\n")
        pq._git(repo, "config", "status.showUntrackedFiles", "no")
    elif change in {"unpushed", "empty-unpushed"}:
        pq._commit(repo, "local.txt")
        if change == "empty-unpushed":
            pq._git(repo, "rm", "-q", "-f", "a.txt", "b.txt", "hidden.txt", "local.txt")
    elif change == "staged":
        pq._git(repo, "add", "-u")
    elif change.startswith("hidden-"):
        flag = "--assume-unchanged" if change == "hidden-assume" else "--skip-worktree"
        pq._git(repo, "update-index", flag, "hidden.txt")
        (repo / "hidden.txt").write_text("hidden work\n")
    elif change == "no-upstream":
        pq._git(repo, "branch", "--unset-upstream")
    elif change == "detached":
        pq._git(repo, "checkout", "-q", "--detach")
    elif change == "mixed":
        pq._git(repo, "rm", "-q", "b.txt")
    tid = kb.create_task(w2.conn, title="clone with private state", assignee="default", runs_on=[pq.POOL])
    with kb.write_txn(w2.conn):
        w2.conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(ws), tid))
    pq._run(w2, monkeypatch, tid)
    step = kpp.plan(w2.conn, tid)
    assert step is not None and step["op"] == "pin" and str(repo) in step["state"]


@pytest.mark.parametrize("empty_index", [False, True])
def test_upstream_may_be_ahead(tmp_path, empty_index):
    ws, repo = _checkout(tmp_path, empty_index=empty_index)
    pr = ws / "polyworld"
    pq._commit(pr, "ahead.txt")
    pq._git(pr, "push", "-q", "origin", "HEAD:wt/x")
    assert kmp.tree_state([str(repo)]) is None


@pytest.mark.parametrize("probe_name", ["status", "symbolic-ref", "merge-base", "ls-files"])
@pytest.mark.parametrize("failure", ["exit", "stderr", "timeout"])
def test_unknown_probe_still_pins(tmp_path, monkeypatch, probe_name, failure):
    from hermes_cli import worktree_ops
    _ws, repo = _checkout(tmp_path, empty_index=True)
    real = worktree_ops._git

    def git(args, *a, **kw):
        if args[0] == probe_name and (probe_name != "status" or "--porcelain=v1" in args):
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 10)
            return subprocess.CompletedProcess(args, 128 if failure == "exit" else 0, "", "warning: unknown")
        return real(args, *a, **kw)

    monkeypatch.setattr(worktree_ops, "_git", git)
    assert kmp.tree_state([str(repo)]) is not None
