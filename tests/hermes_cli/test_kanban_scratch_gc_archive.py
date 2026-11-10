"""Aged terminal scratch holding git clones is archived, then removed. [daveey]"""
import argparse
import json
import os
import subprocess
import tarfile
import time
from pathlib import Path

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_ops

OLD = int(time.time()) - 10 * 86400


@pytest.fixture
def board(tmp_path, monkeypatch):
    # No external jobs belong to this synthetic board. Host cwd probes can stat
    # deleted directories under the real Hermes home, outside fixture isolation.
    monkeypatch.setattr(psutil, "process_iter", lambda *args, **kwargs: iter(()))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACES_ROOT", raising=False)
    return tmp_path


def _run(*args, cwd=None):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    src = tmp_path / "seed"
    src.mkdir()
    _run("init", "-q", cwd=src)
    (src / "tracked.txt").write_text("one\n")
    _run("add", ".", cwd=src)
    _run("commit", "-q", "-m", "seed", cwd=src)
    bare = tmp_path / "remote.git"
    _run("clone", "-q", "--bare", str(src), str(bare))
    return bare


def _task(status="done", completed_at=OLD, clone_into="repo", remote=None):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="scratch with clone")
        path = kb.workspaces_root() / tid
        path.mkdir(parents=True)
        (path / "notes.txt").write_text("scratch")
        if remote is not None:
            target = path if clone_into == "." else path / clone_into
            if clone_into == ".":
                _run("clone", "-q", str(remote), str(target / "tmpclone"))
                for entry in (target / "tmpclone").iterdir():
                    entry.rename(target / entry.name)
                (target / "tmpclone").rmdir()
            else:
                _run("clone", "-q", str(remote), str(target))
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status=?, workspace_kind='scratch', workspace_path=?, "
                "completed_at=?, created_at=? WHERE id=?",
                (status, str(path), completed_at, OLD, tid),
            )
    return tid, path


def _gc(days=7):
    return kanban_ops._cmd_gc(argparse.Namespace(
        event_retention_days=0, log_retention_days=0, worktree_retention_days=days,
    ))


def _archive(tid, board_slug="default"):
    return kb.kanban_home() / "archive" / "scratch" / board_slug / tid


def _events(tid):
    with kbc.connect_closing() as conn:
        return [(r["kind"], json.loads(r["payload"] or "null"))
                for r in conn.execute("SELECT kind, payload FROM task_events WHERE task_id=?", (tid,))]


def test_clean_pushed_clone_is_archived_and_removed(board, remote):
    tid, path = _task(remote=remote)
    assert _gc() == 0
    assert not path.exists()
    meta = json.loads((_archive(tid) / "repo" / "meta.json").read_text())
    assert meta["task_id"] == tid and meta["completed_at"] == OLD
    assert meta["branch"] == "main" and meta["head"]
    assert meta["remotes"]["origin"] == str(remote)
    assert sorted(os.listdir(_archive(tid) / "repo")) == ["meta.json"]
    archived = [p for k, p in _events(tid) if k == "workspace_archived"]
    assert archived == [{"path": str(path), "archive": str(_archive(tid))}]
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".")]


def test_uncommitted_and_untracked_land_in_diff_and_tar(board, remote):
    tid, path = _task(remote=remote)
    (path / "repo" / "tracked.txt").write_text("one\nlocal edit\n")
    (path / "repo" / "new.txt").write_text("brand new\n")
    (path / "repo" / "node_modules").symlink_to(board)  # never followed
    assert _gc() == 0
    assert not path.exists()
    dest = _archive(tid) / "repo"
    assert "+local edit" in (dest / "tracked.diff").read_text()
    with tarfile.open(dest / "untracked.tar") as tar:
        names = tar.getnames()
        assert "new.txt" in names
        assert tar.extractfile("new.txt").read() == b"brand new\n"
        assert tar.getmember("node_modules").issym()


def test_unpushed_commit_lands_in_verified_bundle(board, remote, tmp_path):
    tid, path = _task(remote=remote)
    repo = path / "repo"
    (repo / "tracked.txt").write_text("two\n")
    _run("commit", "-q", "-am", "unpushed work", cwd=repo)
    sha = _run("rev-parse", "HEAD", cwd=repo)
    assert _gc() == 0
    assert not path.exists()
    bundle = _archive(tid) / "repo" / "commits.bundle"
    check = tmp_path / "check"
    _run("clone", "-q", str(remote), str(check))
    _run("bundle", "verify", str(bundle), cwd=check)
    assert sha in _run("bundle", "list-heads", str(bundle), cwd=check)


def test_workspace_that_is_itself_a_clone(board, remote):
    tid, path = _task(remote=remote, clone_into=".")
    (path / "extra.txt").write_text("x")
    assert _gc() == 0
    assert not path.exists()
    dest = _archive(tid)
    assert json.loads((dest / "meta.json").read_text())["task_id"] == tid
    with tarfile.open(dest / "untracked.tar") as tar:
        assert {"extra.txt", "notes.txt"} <= set(tar.getnames())


def test_young_workspace_is_kept(board, remote):
    _, path = _task(remote=remote, completed_at=int(time.time()) - 3600)
    assert _gc() == 0
    assert (path / "repo" / ".git").is_dir()


def test_retention_zero_keeps_it(board, remote):
    tid, path = _task(remote=remote)
    assert _gc(days=0) == 0
    assert (path / "repo" / ".git").is_dir()
    assert not _archive(tid).exists()


@pytest.mark.parametrize("status", ["ready", "running", "blocked", "review"])
def test_nonterminal_card_keeps_it(board, remote, status):
    tid, path = _task(remote=remote, status=status)
    assert _gc() == 0
    assert (path / "repo" / ".git").is_dir()
    assert not _archive(tid).exists()


def test_active_child_keeps_it(board, remote):
    tid, path = _task(remote=remote)
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="needs artifacts", parents=[tid])
    assert _gc() == 0
    assert (path / "repo" / ".git").is_dir()
    assert not _archive(tid).exists()


def test_live_sharer_on_another_board_keeps_it(board, remote):
    tid, path = _task(remote=remote)
    kb.create_board("other")
    with kbc.connect_closing(board="other") as conn:
        sharer = kb.create_task(conn, title="live sharer")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(path), sharer))
    assert _gc() == 0
    assert (path / "repo" / ".git").is_dir()
    assert not _archive(tid).exists()


def test_linked_worktree_inside_keeps_it(board, remote):
    tid, path = _task(remote=remote)
    _run("worktree", "add", "-q", "-b", "side", str(path / "wt"), cwd=path / "repo")
    assert (path / "wt" / ".git").is_file()
    assert _gc() == 0
    assert (path / "repo" / ".git").is_dir() and (path / "wt").is_dir()
    assert not _archive(tid).exists()


def test_bare_repo_inside_keeps_it(board, remote):
    tid, path = _task(remote=remote)
    _run("clone", "-q", "--bare", str(remote), str(path / "mirror.git"))
    assert _gc() == 0
    assert (path / "repo" / ".git").is_dir()
    assert not _archive(tid).exists()


def test_unwritable_archive_dir_keeps_everything(board, remote):
    tid, path = _task(remote=remote)
    (path / "repo" / "new.txt").write_text("keep me\n")
    root = kb.kanban_home() / "archive" / "scratch"
    root.mkdir(parents=True)
    root.chmod(0o500)
    try:
        assert _gc() == 0
    finally:
        root.chmod(0o700)
    assert (path / "repo" / "new.txt").read_text() == "keep me\n"
    assert (path / "notes.txt").exists()
    assert list(root.iterdir()) == []
    assert not [k for k, _ in _events(tid) if k == "workspace_archived"]


def test_failed_archive_step_keeps_it_and_leaves_no_partial(board, remote, monkeypatch):
    from hermes_cli import kanban_scratch_archive as sa

    tid, path = _task(remote=remote)
    real = sa._git

    def broken(repo, env, *args, **kw):
        if args[0] == "ls-files":
            raise sa.ArchiveRefused("injected")
        return real(repo, env, *args, **kw)

    monkeypatch.setattr(sa, "_git", broken)
    assert _gc() == 0
    assert (path / "repo" / ".git").is_dir()
    parent = _archive(tid).parent
    assert not parent.exists() or list(parent.iterdir()) == []


def test_named_board_archives_under_its_slug(board, remote):
    kb.create_board("other")
    with kbc.connect_closing(board="other") as conn:
        tid = kb.create_task(conn, title="named board clone")
        path = kb.workspaces_root(board="other") / tid
        path.mkdir(parents=True)
        _run("clone", "-q", str(remote), str(path / "repo"))
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done', workspace_kind='scratch', workspace_path=?, "
                         "completed_at=? WHERE id=?", (str(path), OLD, tid))
        from hermes_cli.kanban_worktree_gc import gc_task_scratch
        assert gc_task_scratch(conn, older_than_days=7) == 1
    assert not path.exists()
    assert (_archive(tid, "other") / "repo" / "meta.json").is_file()
