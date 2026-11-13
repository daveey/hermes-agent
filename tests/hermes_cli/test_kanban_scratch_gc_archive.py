"""Aged terminal scratch holding git clones is archived, then removed. [daveey]"""
import argparse
import json
import os
import subprocess
import sys
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
    # maintenance.auto/gc.auto off: a fixture commit must not leave a detached
    # `git maintenance run --auto` writing objects/maintenance.lock under a
    # tree the scratch archive is about to scan (it would fail closed).
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "init.defaultBranch=main",
         "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    return _seed_remote(tmp_path)


def _seed_remote(tmp_path):
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


def test_fixture_git_never_spawns_background_maintenance(board, tmp_path, monkeypatch):
    # A commit inside a scanned clone would otherwise launch `git maintenance run
    # --auto`; git >= 2.47 detaches it, so objects/maintenance.lock can come and go
    # while _gc scans the tree and the archive fails closed ("scan uncertain").
    trace = tmp_path / "git-trace2.jsonl"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    _, path = _task(remote=_seed_remote(tmp_path))
    (path / "repo" / "tracked.txt").write_text("two\n")
    _run("commit", "-q", "-am", "unpushed work", cwd=path / "repo")
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    assert any(e.get("event") == "start" and "commit" in e.get("argv", []) for e in events)
    maintenance = [
        e for e in events
        if e.get("event") == "child_start" and e.get("argv", [])[:2] in (["git", "maintenance"], ["git", "gc"])
    ]
    assert not maintenance, maintenance


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


# --- complete filesystem payload (non-git trees, ignored/sibling files) ---------------

PAYLOAD = ".hermes-complete-archive-v1"


def _members(tid):
    with tarfile.open(_archive(tid) / PAYLOAD / "tree.tar.gz") as tar:
        return {m.name: (m, tar.extractfile(m).read() if m.isfile() else None) for m in tar.getmembers()}


def _plain_task(build):
    tid, path = _task()
    (path / "link").symlink_to("notes.txt")  # symlink makes the tree non-removable -> archive path
    build(path)
    return tid, path


def test_non_git_symlink_tree_is_archived_completely_then_removed(board):
    outside = board / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("must not be copied")

    def build(p):
        (p / "empty").mkdir()
        (p / "deep" / "er").mkdir(parents=True)
        (p / "deep" / "er" / "run.sh").write_text("#!/bin/sh\n")
        (p / "deep" / "er" / "run.sh").chmod(0o750)
        (p / "home").symlink_to(outside)
        (p / "dangling").symlink_to("nowhere/at/all")

    tid, path = _plain_task(build)
    assert _gc() == 0
    assert not path.exists()
    m = _members(tid)
    assert m["empty"][0].isdir() and m["deep/er/run.sh"][0].mode == 0o750
    assert m["deep/er/run.sh"][1] == b"#!/bin/sh\n" and m["notes.txt"][1] == b"scratch"
    assert m["home"][0].issym() and m["home"][0].linkname == str(outside)
    assert m["dangling"][0].issym() and m["dangling"][0].linkname == "nowhere/at/all"
    assert not any(n.startswith("home/") for n in m)
    assert (outside / "secret.txt").read_text() == "must not be copied"
    from hermes_cli import kanban_scratch_archive as sa
    sa.verify_complete_archive(_archive(tid))


def test_mixed_git_tree_keeps_ignored_and_sibling_files(board, remote):
    tid, path = _task(remote=remote)
    (path / "repo" / ".gitignore").write_text("cache/\n")
    (path / "repo" / "cache").mkdir()
    (path / "repo" / "cache" / "blob.bin").write_bytes(b"\x00\x01ignored")
    (path / "sibling").mkdir()
    (path / "sibling" / "evidence.txt").write_text("only here")
    assert _gc() == 0
    assert not path.exists()
    m = _members(tid)
    assert m["repo/cache/blob.bin"][1] == b"\x00\x01ignored"
    assert m["sibling/evidence.txt"][1] == b"only here"
    assert (_archive(tid) / "repo" / "meta.json").is_file()  # existing layout untouched


def test_source_mutation_during_archive_keeps_workspace(board, monkeypatch):
    tid, path = _plain_task(lambda p: (p / "deep").mkdir())
    real = tarfile.TarFile.addfile
    state = {"done": False}

    def racing(self, tarinfo, fileobj=None):
        out = real(self, tarinfo, fileobj)
        if not state["done"] and tarinfo.isfile():
            state["done"] = True
            (path / "deep" / "late.txt").write_text("written mid-archive")
        return out

    monkeypatch.setattr(tarfile.TarFile, "addfile", racing)
    assert _gc() == 0
    assert (path / "deep" / "late.txt").read_text() == "written mid-archive"
    assert (path / "notes.txt").exists()
    parent = _archive(tid).parent
    assert not parent.exists() or list(parent.iterdir()) == []


def test_nested_rewrite_of_archived_file_keeps_workspace(board, monkeypatch):
    tid, path = _plain_task(lambda p: ((p / "a").mkdir(), (p / "a" / "f.txt").write_text("v1")))
    real = tarfile.TarFile.close

    def racing(self):
        real(self)
        (path / "a" / "f.txt").write_text("v2-longer")

    monkeypatch.setattr(tarfile.TarFile, "close", racing)
    assert _gc() == 0
    assert (path / "a" / "f.txt").read_text() == "v2-longer"
    assert not _archive(tid).exists()


def test_payload_write_failure_keeps_workspace(board, monkeypatch):
    tid, path = _plain_task(lambda p: None)

    def boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(tarfile.TarFile, "addfile", boom)
    assert _gc() == 0
    assert (path / "notes.txt").read_text() == "scratch"
    parent = _archive(tid).parent
    assert not parent.exists() or list(parent.iterdir()) == []


def test_special_file_keeps_workspace(board):
    tid, path = _plain_task(lambda p: os.mkfifo(p / "pipe"))
    assert _gc() == 0
    assert (path / "notes.txt").exists()
    assert not _archive(tid).exists()


def test_reserved_metadata_name_collision_keeps_workspace(board):
    tid, path = _plain_task(lambda p: (p / PAYLOAD).mkdir())
    assert _gc() == 0
    assert (path / PAYLOAD).is_dir() and (path / "notes.txt").exists()
    assert not _archive(tid).exists()


def test_verify_rejects_tampered_payload(board):
    from hermes_cli import kanban_scratch_archive as sa
    tid, _path = _plain_task(lambda p: None)
    assert _gc() == 0
    sa.verify_complete_archive(_archive(tid))
    tar_path = _archive(tid) / PAYLOAD / "tree.tar.gz"
    tar_path.write_bytes(tar_path.read_bytes()[:-20])
    with pytest.raises(sa.ArchiveRefused):
        sa.verify_complete_archive(_archive(tid))


# --- snapshot enforcement at detach, ctime, descriptor traversal -------------------------

def _hook_check_snapshot(monkeypatch, when, action):
    """Run *action(path)* just before the *when*-th (0-based) check_snapshot call."""
    from hermes_cli import kanban_scratch_archive as sa
    real, calls = sa.check_snapshot, []

    def wrapper(path, snapshot, **kw):
        if len(calls) == when:
            action(Path(path))
        calls.append(path)
        return real(path, snapshot, **kw)

    monkeypatch.setattr(sa, "check_snapshot", wrapper)
    return calls


@pytest.mark.parametrize("when", [0, 1], ids=["pre_detach", "detached_before_rmtree"])
def test_nested_change_after_archive_keeps_or_restores_workspace(board, monkeypatch, when):
    _tid, path = _plain_task(lambda p: ((p / "a" / "b").mkdir(parents=True), (p / "a" / "b" / "f").write_text("v1")))
    calls = _hook_check_snapshot(monkeypatch, when, lambda root: (root / "a" / "b" / "late.txt").write_text("late"))
    assert _gc() == 0
    assert len(calls) == when + 1
    assert (path / "a" / "b" / "late.txt").read_text() == "late"
    assert (path / "notes.txt").exists()
    assert not [p for p in path.parent.iterdir() if ".gc-" in p.name]


def test_equal_size_rewrite_with_restored_mtime_is_detected(board, monkeypatch):
    tid, path = _plain_task(lambda p: (p / "a.txt").write_text("v1"))
    ext = board / "hardlink-ext"
    os.link(path / "a.txt", ext)  # rewrite arrives through a second hardlink
    st = (path / "a.txt").stat()
    real = tarfile.TarFile.close

    def racing(self):
        real(self)
        ext.write_text("v2")  # same size
        os.utime(ext, ns=(st.st_atime_ns, st.st_mtime_ns))  # mtime restored; ctime cannot be

    monkeypatch.setattr(tarfile.TarFile, "close", racing)
    assert _gc() == 0
    assert (path / "a.txt").read_text() == "v2"
    assert not _archive(tid).exists()


def test_equal_size_rewrite_after_return_is_detected_at_detach(board, monkeypatch):
    _tid, path = _plain_task(lambda p: (p / "a.txt").write_text("v1"))
    st = (path / "a.txt").stat()

    def rewrite(root):
        (root / "a.txt").write_text("v2")
        os.utime(root / "a.txt", ns=(st.st_atime_ns, st.st_mtime_ns))

    _hook_check_snapshot(monkeypatch, 0, rewrite)
    assert _gc() == 0
    assert (path / "a.txt").read_text() == "v2"


def test_ancestor_directory_swapped_for_external_symlink_is_not_followed(board, monkeypatch):
    outside = board / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("must not be read")
    tid, path = _plain_task(lambda p: ((p / "deep").mkdir(), (p / "deep" / "f.txt").write_text("x")))
    real = tarfile.TarFile.addfile
    state = {"done": False}

    def racing(self, tarinfo, fileobj=None):
        out = real(self, tarinfo, fileobj)
        if not state["done"] and tarinfo.name == "deep":
            state["done"] = True
            import shutil
            shutil.rmtree(path / "deep")
            (path / "deep").symlink_to(outside)
        return out

    monkeypatch.setattr(tarfile.TarFile, "addfile", racing)
    assert _gc() == 0
    assert state["done"] and (path / "deep").is_symlink()
    assert (outside / "secret.txt").read_text() == "must not be read"
    assert not _archive(tid).exists()


def test_scan_refuses_symlink_root_and_swapped_ancestor(board):
    from hermes_cli import kanban_scratch_archive as sa
    real = board / "real"
    (real / "d").mkdir(parents=True)
    (board / "link").symlink_to(real)
    with pytest.raises(sa.ArchiveRefused):
        sa._scan(board / "link")
    with pytest.raises(sa.ArchiveRefused):
        sa.find_repos(board / "link")
    # O_NOFOLLOW on the final component alone does not reject this path.
    with pytest.raises(sa.ArchiveRefused):
        sa._scan(board / "link" / "d")
    with pytest.raises(sa.ArchiveRefused):
        sa.find_repos(board / "link" / "d")


def test_regular_file_replaced_by_fifo_before_open_refuses_without_blocking(board, monkeypatch):
    from hermes_cli import kanban_scratch_archive as sa
    root = board / "fifo-race"
    root.mkdir()
    victim = root / "file"
    victim.write_text("original")
    real_open = os.open
    raced = []

    def racing(name, flags, *args, **kwargs):
        if name == "file" and kwargs.get("dir_fd") is not None:
            # Assert before opening: a regression must fail, not hang the suite.
            assert flags & os.O_NONBLOCK
            victim.unlink()
            os.mkfifo(victim)
            raced.append(True)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing)
    with tarfile.open(board / "fifo-race.tar", "w") as tar:
        with pytest.raises(sa.ArchiveRefused, match="changed during archive"):
            sa._scan(root, tar, {})
    assert raced == [True]


@pytest.mark.parametrize("race", ["none", "pickup", "reoccupied", "job", "mutation"])
def test_archive_scans_outside_transactions_and_rechecks_after_postscan(board, monkeypatch, race):
    from hermes_cli import kanban_scratch_archive as sa
    from hermes_cli import kanban_worktree_gc as gc
    tid, path = _plain_task(lambda p: (p / "nested").mkdir())
    with kbc.connect_closing() as conn:
        scan, check = sa._scan, sa.check_snapshot
        calls = []
        job = []

        def scan_unlocked(*args, **kwargs):
            assert not conn.in_transaction, "recursive archive scan holds the Board writer lock"
            return scan(*args, **kwargs)

        def postscan(tree, snapshot, **kwargs):
            assert not conn.in_transaction
            check(tree, snapshot, **kwargs)
            calls.append(tree)
            if kwargs.get("renamed"):
                def pickup():
                    # Use a second connection: proves a pickup can commit while scanning.
                    with kbc.connect_closing() as other, kb.write_txn(other):
                        other.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))

                def mutate():
                    (tree / "nested" / "late.txt").write_text("late")
                    check(tree, snapshot, **kwargs)

                actions = {"none": lambda: None, "pickup": pickup,
                           "reoccupied": path.mkdir, "job": lambda: job.append(True),
                           "mutation": mutate}
                actions[race]()

        monkeypatch.setattr(sa, "_scan", scan_unlocked)
        monkeypatch.setattr(sa, "check_snapshot", postscan)
        monkeypatch.setattr(gc, "defer_owned_job_cleanup", lambda *_: bool(job))
        assert gc._archive_and_remove_scratch(conn, tid, path, OLD) == (race == "none")
        assert len(calls) == 2
        events = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='workspace_archived'",
                              (tid,)).fetchall()
        assert bool(events) == (race == "none")
        if race == "pickup":
            assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "running"
        intents = list(_archive(tid).glob("detach-*.json"))
        if race == "none":
            assert not path.exists() and not intents
        elif race == "reoccupied":
            assert path.is_dir() and list(path.iterdir()) == []
            record = json.loads(intents[0].read_text())
            assert record["original"] == str(path)
            assert (Path(record["detached"]) / "notes.txt").read_text() == "scratch"
        else:
            assert (path / "notes.txt").read_text() == "scratch"
            if race == "mutation":
                assert (path / "nested" / "late.txt").read_text() == "late"


@pytest.mark.platforms("linux")
def test_process_crash_after_detach_leaves_recoverable_intent(board):
    import sys
    from tools.environments.local import build_subprocess_env
    from hermes_cli import kanban_scratch_archive as sa
    from hermes_cli import kanban_worktree_gc as gc
    tid, path = _plain_task(lambda p: None)
    script = """
import os
import sys
from pathlib import Path
from hermes_cli import kanban_scratch_archive as sa
from hermes_cli import kanban_db_connect as kbc
path, dest = map(Path, sys.argv[1:3])
saved = sa.archive_workspace(path, dest, task_id=path.name, completed_at=None)
real = sa.check_snapshot
def crash(tree, snapshot, **kwargs):
    if kwargs.get('renamed'):
        os._exit(79)  # no finally/restore: abrupt process death, not a Python exception
    real(tree, snapshot, **kwargs)
sa.check_snapshot = crash
with kbc.connect_closing() as conn:
    sa.remove_archived(conn, path, saved, lambda tree: True, lambda: None)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), str(_archive(tid))],
        env=build_subprocess_env(extra={"HERMES_HOME": str(kb.kanban_home()), "HOME": str(board)}),
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )
    assert result.returncode == 79, result.stderr
    assert not path.exists()
    intents = list(_archive(tid).glob("detach-*.json"))
    assert len(intents) == 1
    record = json.loads(intents[0].read_text())
    detached = Path(record["detached"])
    assert record["original"] == str(path) and record["archive"] == str(_archive(tid))
    assert detached.stat().st_ino == record["root_identity"][4]
    assert (detached / "notes.txt").read_text() == "scratch"
    sa.verify_complete_archive(Path(record["archive"]))
    # A subsequent pass must not guess that this unowned hidden tree is deletable.
    with kbc.connect_closing() as conn:
        assert gc.gc_task_scratch(conn, older_than_days=7) == 0
    assert detached.is_dir() and intents[0].exists()
    sa._rename_noreplace(detached, path)  # demonstrate receipt-guided manual recovery
    assert (path / "notes.txt").read_text() == "scratch"


@pytest.mark.parametrize("failure", ["intent_fsync", "commit", "remove", "restore"])
def test_archive_detach_faults_preserve_recovery_evidence(board, monkeypatch, failure):
    import contextlib
    import sqlite3
    from hermes_cli import kanban_scratch_archive as sa
    tid, path = _plain_task(lambda p: None)
    saved = sa.archive_workspace(path, _archive(tid), task_id=tid, completed_at=OLD)
    real_txn = kb.write_txn
    calls = []

    @contextlib.contextmanager
    def fail_final_commit(conn):
        with real_txn(conn):
            calls.append(True)
            yield
            if len(calls) == 2:
                raise sqlite3.OperationalError("injected final commit failure")

    def fail(*args, **kwargs):
        raise OSError("injected filesystem failure")

    if failure == "intent_fsync":
        monkeypatch.setattr(sa.os, "fsync", fail)
    elif failure == "commit":
        monkeypatch.setattr(kb, "write_txn", fail_final_commit)
    elif failure == "remove":
        monkeypatch.setattr(sa.shutil, "rmtree", fail)
    else:
        real_check, real_rename = sa.check_snapshot, sa._rename_noreplace

        def change(tree, snapshot, **kwargs):
            if kwargs.get("renamed"):
                (tree / "late.txt").write_text("unarchived")
            real_check(tree, snapshot, **kwargs)

        def cannot_restore(source, dest):
            if dest == path:
                fail()
            real_rename(source, dest)

        monkeypatch.setattr(sa, "check_snapshot", change)
        monkeypatch.setattr(sa, "_rename_noreplace", cannot_restore)
    with kbc.connect_closing() as conn:
        def finalize():
            conn.execute("UPDATE tasks SET title='finalized' WHERE id=?", (tid,))

        with pytest.raises((OSError, sqlite3.Error, sa.ArchiveRefused)):
            sa.remove_archived(conn, path, saved, lambda tree: True, finalize)
        title = conn.execute("SELECT title FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        assert (title == "finalized") == (failure == "remove")
    intents = list(saved.glob("detach-*.json"))
    assert len(intents) == 1
    record = json.loads(intents[0].read_text())
    if failure in ("intent_fsync", "commit"):
        assert (path / "notes.txt").read_text() == "scratch"
        assert not Path(record["detached"]).exists()
    else:
        assert not path.exists()
        assert (Path(record["detached"]) / "notes.txt").read_text() == "scratch"
        if failure == "restore":
            assert (Path(record["detached"]) / "late.txt").read_text() == "unarchived"


@pytest.mark.parametrize("when", [0, 1], ids=["pre_detach", "postscan"])
def test_root_replaced_after_scan_is_never_removed(board, monkeypatch, when):
    from hermes_cli import kanban_scratch_archive as sa
    tid, path = _plain_task(lambda p: None)
    saved = sa.archive_workspace(path, _archive(tid), task_id=tid, completed_at=OLD)
    check, calls = sa.check_snapshot, []
    preserved = path.with_name(path.name + "-preserved")

    def swap_after_scan(tree, snapshot, **kwargs):
        check(tree, snapshot, **kwargs)
        if len(calls) == when:
            tree.rename(preserved)
            tree.mkdir()
        calls.append(tree)

    def forbidden(*args, **kwargs):
        pytest.fail("must not remove a root whose identity changed after scan")

    monkeypatch.setattr(sa, "check_snapshot", swap_after_scan)
    monkeypatch.setattr(sa.shutil, "rmtree", forbidden)
    with kbc.connect_closing() as conn:
        if when:
            with pytest.raises(sa.ArchiveRefused):
                sa.remove_archived(conn, path, saved, lambda tree: True, forbidden)
        else:
            assert sa.remove_archived(conn, path, saved, lambda tree: True, forbidden) == 0
    assert (preserved / "notes.txt").read_text() == "scratch"
    assert path.is_dir() and list(path.iterdir()) == []


# ---- review fixes: durability before publication, raw-row path equality -------------------------------


def _fd_path(fd):
    """Resolved path of an open descriptor: /proc on Linux, F_GETPATH on macOS (which has no /proc)."""
    if sys.platform == "darwin":
        import fcntl

        return os.path.realpath(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0").decode())
    return os.path.realpath(os.readlink(f"/proc/self/fd/{fd}"))


def _real(path):
    """Expected path in _fd_path's resolved form (macOS reports /private/var for /var)."""
    return os.path.realpath(str(path))


@pytest.mark.platforms("posix")  # macOS lane runs _fd_path's F_GETPATH branch
def test_archive_fsyncs_all_artifacts_bottom_up_before_publication_and_detach(board, monkeypatch):
    from hermes_cli import kanban_scratch_archive as sa

    tid, path = _plain_task(lambda p: ((p / "sub").mkdir(), (p / "sub" / "f.txt").write_text("x")))
    events = []
    real_fsync, real_detach, real_rename = os.fsync, sa._rename_noreplace, Path.rename

    def spy_fsync(fd):
        events.append(("fsync", _fd_path(fd)))
        return real_fsync(fd)

    def spy_detach(src, dst):
        events.append(("detach", str(src)))
        return real_detach(src, dst)

    def spy_publish(self, target):
        if ".partial-" in self.name:
            events.append(("publish", str(self)))
        return real_rename(self, target)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(sa, "_rename_noreplace", spy_detach)
    monkeypatch.setattr(Path, "rename", spy_publish)
    assert _gc() == 0 and not path.exists()
    kinds = [k for k, _ in events]
    pub, det = kinds.index("publish"), kinds.index("detach")
    assert pub < det
    partial = events[pub][1]
    before = [p for k, p in events[:pub] if k == "fsync"]
    tree = next(i for i, p in enumerate(before) if p.endswith("tree.tar.gz"))
    manifest = next(i for i, p in enumerate(before) if p.endswith("manifest.json"))
    payload_dir = before.index(_real(Path(partial) / sa.PAYLOAD_DIR))
    assert max(tree, manifest) < payload_dir < before.index(_real(partial))  # files, then dir, then parent dir
    assert _real(_archive(tid).parent) in [p for k, p in events[pub:det] if k == "fsync"]


@pytest.mark.parametrize("existing", [False, True], ids=["absent_root", "retry_existing_chain"])
def test_archive_flushes_ancestor_entries_before_return(tmp_path, monkeypatch, existing):
    from hermes_cli import kanban_scratch_archive as sa

    source = tmp_path / "source"
    source.mkdir()
    (source / "evidence").write_text("unique")
    dest = tmp_path / "archive" / "scratch" / "new-board" / "task"
    if existing:
        dest.parent.mkdir(parents=True)
    flushed = []
    real_flush = sa._fsync_path

    def flush(path, *, directory):
        real_flush(path, directory=directory)
        if directory:
            flushed.append(path)

    monkeypatch.setattr(sa, "_fsync_path", flush)
    result = sa.archive_workspace(source, dest, task_id="task", completed_at=OLD)
    sa.verify_complete_archive(result)
    assert all(parent in flushed for parent in dest.absolute().parents)


@pytest.mark.parametrize("ancestor", [0, 1, 2])
def test_ancestor_fsync_failure_preserves_workspace(board, monkeypatch, ancestor):
    from hermes_cli import kanban_scratch_archive as sa

    tid, path = _plain_task(lambda p: None)
    target = list(_archive(tid).parent.parents)[ancestor]
    real_flush = sa._fsync_path
    hits = []

    def flush(current, *, directory):
        if directory and current == target:
            hits.append(current)
            raise OSError(5, "ancestor EIO")
        return real_flush(current, directory=directory)

    monkeypatch.setattr(sa, "_fsync_path", flush)
    assert _gc() == 0
    assert hits
    assert (path / "notes.txt").read_text() == "scratch"
    assert not _archive(tid).exists()


@pytest.mark.platforms("posix")  # macOS lane runs _fd_path's F_GETPATH branch
@pytest.mark.parametrize("target", ["file", "directory", "publication_parent"])
def test_fsync_failure_fails_closed_without_publication(board, monkeypatch, target):
    from hermes_cli import kanban_scratch_archive as sa

    tid, path = _plain_task(lambda p: None)
    real_fsync = os.fsync
    hits = []

    def flaky(fd):
        link = _fd_path(fd)
        hit = {"file": link.endswith("manifest.json"), "directory": link.endswith(sa.PAYLOAD_DIR),
               "publication_parent": link == _real(_archive(tid).parent)}[target]
        if hit:
            hits.append(link)
            raise OSError(5, "EIO")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", flaky)
    assert _gc() == 0
    # The injected EIO, not a broken _fd_path (whose own OSError also fails closed), stopped the archive.
    assert hits
    assert (path / "notes.txt").read_text() == "scratch"
    parent = _archive(tid).parent
    assert not parent.exists() or list(parent.iterdir()) == []


@pytest.mark.parametrize("raw", [None, "~/hermes/kanban/workspaces/{tid}"], ids=["null_path", "tilde_path"])
def test_null_or_tilde_row_path_is_archived_and_repeat_gc_is_idempotent(board, monkeypatch, raw):
    monkeypatch.setenv("HOME", str(board))
    tid, path = _plain_task(lambda p: None)
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (raw.format(tid=tid) if raw else None, tid))
    assert _gc() == 0
    assert not path.exists()
    assert (_archive(tid) / PAYLOAD / "manifest.json").exists()
    assert [k for k, _ in _events(tid)].count("workspace_archived") == 1
    assert _gc() == 0
    assert [k for k, _ in _events(tid)].count("workspace_archived") == 1


# ---- reclaim throughput: uv's .git cache marker is data; fast gzip level ---------------------------


def _uv_cache(p, marker=b""):
    cache = p / "test-home" / ".cache" / "uv" / "sdists-v10"
    cache.mkdir(parents=True)
    (cache / ".git").write_bytes(marker)
    (cache / ".gitignore").write_text("*\n")
    (cache / "pkg.tar.gz").write_bytes(b"\x1f\x8bsdist")
    return cache


@pytest.mark.parametrize("marker", [b"", b"not a pointer\n"], ids=["empty", "no_gitdir_line"])
def test_uv_git_marker_file_is_archived_as_data_and_round_trips(board, tmp_path, marker):
    from hermes_cli import kanban_scratch_archive as sa

    tid, path = _plain_task(lambda p: _uv_cache(p, marker))
    assert sa.find_repos(path) == []
    assert _gc() == 0
    assert not path.exists()
    rel = "test-home/.cache/uv/sdists-v10"
    m = _members(tid)
    assert m[f"{rel}/.git"][0].isfile() and m[f"{rel}/.git"][1] == marker
    sa.verify_complete_archive(_archive(tid))
    out = tmp_path / "restored"
    with tarfile.open(_archive(tid) / PAYLOAD / "tree.tar.gz", "r:gz") as tar:
        tar.extractall(out, filter="tar")
    assert (out / rel / ".git").read_bytes() == marker
    assert (out / rel / ".gitignore").read_text() == "*\n"
    assert (out / rel / "pkg.tar.gz").read_bytes() == b"\x1f\x8bsdist"


@pytest.mark.parametrize("pointer", [b"gitdir: /src/repo/.git/worktrees/wt\n", b"  gitdir:/src/repo/.git/modules/m"],
                         ids=["worktree", "leading_space"])
def test_gitdir_pointer_file_is_still_refused(board, pointer):
    from hermes_cli import kanban_scratch_archive as sa

    tid, path = _plain_task(lambda p: _uv_cache(p, pointer))
    with pytest.raises(sa.ArchiveRefused, match="linked worktree or submodule"):
        sa.find_repos(path)
    assert _gc() == 0
    assert (path / "test-home" / ".cache" / "uv" / "sdists-v10" / ".git").read_bytes() == pointer
    assert not _archive(tid).exists()


def test_git_symlink_is_still_refused_without_reading_its_target(board):
    from hermes_cli import kanban_scratch_archive as sa

    def build(p):
        (p / "empty-target").write_bytes(b"")
        (p / "sub").mkdir()
        (p / "sub" / ".git").symlink_to("../empty-target")

    _tid, path = _plain_task(build)
    with pytest.raises(sa.ArchiveRefused, match="linked worktree or submodule"):
        sa.find_repos(path)
    assert _gc() == 0 and (path / "sub" / ".git").is_symlink()


def test_marker_judged_against_its_scanned_stat(board):
    from hermes_cli import kanban_scratch_archive as sa

    _tid, path = _plain_task(lambda p: _uv_cache(p))
    cache = path / "test-home" / ".cache" / "uv" / "sdists-v10"
    stale = (cache / ".git").lstat()
    dfd = os.open(cache, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert sa._is_gitlink(dfd, cache, stale) is False
        (cache / ".git").write_bytes(b"gitdir: /elsewhere\n")
        with pytest.raises(sa.ArchiveRefused, match="changed during scan"):
            sa._is_gitlink(dfd, cache, stale)
    finally:
        os.close(dfd)


def test_marker_read_through_the_scans_own_directory_fd(board, monkeypatch):
    """Markers are judged via the scan's descriptor: no per-marker re-walk from /."""
    from hermes_cli import kanban_scratch_archive as sa

    def build(p):
        _uv_cache(p)
        (p / "other").mkdir()
        (p / "other" / ".git").write_bytes(b"")

    _tid, path = _plain_task(build)
    opens = []
    real_open_root = sa._open_root
    monkeypatch.setattr(sa, "_open_root", lambda r: opens.append(r) or real_open_root(r))
    assert sa.find_repos(path) == []
    assert opens == [path]  # the scan root only, not once more per .git marker

    # The directory is swapped by path after the scan entered it: the read stays on
    # the directory the scan saw (its marker matches the scanned lstat), instead of
    # following the path to the replacement's gitlink.
    cache = path / "test-home" / ".cache" / "uv" / "sdists-v10"
    real_is_gitlink = sa._is_gitlink

    def swap_then_judge(dfd, directory, st):
        if directory == cache:
            cache.rename(cache.with_name("moved"))
            cache.mkdir()
            (cache / ".git").write_bytes(b"gitdir: /src/repo/.git/worktrees/wt\n")
        return real_is_gitlink(dfd, directory, st)

    monkeypatch.setattr(sa, "_is_gitlink", swap_then_judge)
    repos, markers = sa._find_repos(path)
    assert repos == [] and set(markers) == {"other/.git", "test-home/.cache/uv/sdists-v10/.git"}
    assert (cache.with_name("moved") / ".git").read_bytes() == b""


def test_unjudged_marker_fails_closed(board, monkeypatch):
    """A regular .git the scan never judged is refused, not a KeyError."""
    from hermes_cli import kanban_scratch_archive as sa

    _tid, path = _plain_task(lambda p: _uv_cache(p))
    real_scan = sa._scan
    monkeypatch.setattr(sa, "_scan", lambda root, on_git_file=None, **kw: real_scan(root, **kw))
    with pytest.raises(sa.ArchiveRefused, match="linked worktree or submodule"):
        sa.find_repos(path)


def test_marker_rewritten_into_gitlink_after_judgement_keeps_workspace(board, monkeypatch):
    from hermes_cli import kanban_scratch_archive as sa

    tid, path = _plain_task(lambda p: _uv_cache(p))
    marker = path / "test-home" / ".cache" / "uv" / "sdists-v10" / ".git"
    real = sa._write_payload

    def racing(workspace, dest):
        marker.write_bytes(b"gitdir: /src/repo/.git/worktrees/wt\n")
        return real(workspace, dest)

    monkeypatch.setattr(sa, "_write_payload", racing)
    assert _gc() == 0
    assert marker.read_bytes().startswith(b"gitdir:")
    parent = _archive(tid).parent
    assert not parent.exists() or list(parent.iterdir()) == []


def test_payload_uses_fast_gzip_level_and_old_level_9_archives_still_verify(board, monkeypatch, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa

    assert sa._GZIP_LEVEL == 1
    tid, _path = _plain_task(lambda p: (p / "big.txt").write_text("compressible line\n" * 5000))
    assert _gc() == 0
    tree = _archive(tid) / PAYLOAD / "tree.tar.gz"
    header = tree.read_bytes()[:10]
    assert header[:2] == b"\x1f\x8b" and header[8] == 4  # XFL 4: fastest compression
    assert _members(tid)["big.txt"][1] == b"compressible line\n" * 5000
    sa.verify_complete_archive(_archive(tid))

    # An archive written before this change (default level 9) reads back unchanged.
    source = tmp_path / "legacy-src"
    source.mkdir()
    (source / "f.txt").write_text("legacy\n" * 1000)
    monkeypatch.setattr(sa, "_GZIP_LEVEL", 9)
    legacy = sa.archive_workspace(source, tmp_path / "legacy-archive", task_id="t", completed_at=OLD)
    assert (legacy / PAYLOAD / "tree.tar.gz").read_bytes()[8] == 2  # XFL 2: maximum compression
    sa.verify_complete_archive(legacy)


# --- objects the remote already holds are left out, and restored from it ----------------

FAKE_URL = "https://example.invalid/hermes-fixture.git"


@pytest.fixture(autouse=True)
def _fresh_probe_map(monkeypatch):
    from hermes_cli import kanban_scratch_archive_omit as om
    monkeypatch.setattr(om, "_PROBE_URL_MAP", {})


def _network_clone_task(tmp_path):
    """A done card whose clone's origin is a network URL (served from the local bare repo to the probe and restore)."""
    src = tmp_path / "netseed"
    src.mkdir()
    _run("init", "-q", cwd=src)
    (src / "tracked.txt").write_text("one\n")
    (src / "big.txt").write_text("remote-held line\n" * 20000)
    (src / "bin").mkdir()
    (src / "bin" / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (src / "bin" / "run.sh").chmod(0o755)
    _run("add", ".", cwd=src)
    _run("commit", "-q", "-m", "seed", cwd=src)
    (src / "tracked.txt").write_text("one\ntwo\n")
    _run("commit", "-q", "-am", "second", cwd=src)
    bare = tmp_path / "netremote.git"
    _run("clone", "-q", "--bare", str(src), str(bare))
    tid, path = _task(remote=bare)
    repo = path / "repo"
    _run("remote", "set-url", "origin", FAKE_URL, cwd=repo)
    from hermes_cli import kanban_scratch_archive_omit as om
    om._PROBE_URL_MAP[FAKE_URL] = str(bare)
    return tid, path, repo, bare


def _local_work(repo):
    """Unpushed commit, staged-only blob, unstaged edit, untracked file, stash and a dangling object."""
    (repo / "tracked.txt").write_text("one\ntwo\nunpushed\n")
    _run("commit", "-q", "-am", "unpushed work", cwd=repo)
    (repo / "stashed.txt").write_text("stash me\n")
    _run("add", "stashed.txt", cwd=repo)
    _run("stash", "-q", cwd=repo)
    (repo / "staged.txt").write_text("staged only\n")
    _run("add", "staged.txt", cwd=repo)
    (repo / "staged.txt").write_text("staged then edited\n")
    (repo / "bin" / "run.sh").write_text("#!/bin/sh\necho edited\n")
    (repo / "new.txt").write_text("untracked\n")
    proc = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=b"dangling blob\n",
                          capture_output=True, check=True)
    return proc.stdout.decode().strip()


def _tree_state(root):
    """{rel: (kind, mode, bytes-or-link)} of every entry outside .git/objects (git rewrites those on restore)."""
    out = {}
    for cur, dirs, files in os.walk(root):
        for name in dirs + files:
            full = Path(cur) / name
            rel = full.relative_to(root).as_posix()
            if "/.git/objects/" in f"/{rel}/" and not rel.endswith(".git/objects"):
                continue
            st = full.lstat()
            if full.is_symlink():
                out[rel] = ("link", os.readlink(full))
            elif full.is_dir():
                out[rel] = ("dir", st.st_mode & 0o7777)
            else:
                out[rel] = ("file", st.st_mode & 0o7777, full.read_bytes())
    return out


def test_remote_held_objects_are_omitted_and_restore_round_trips(board, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa
    from hermes_cli import kanban_scratch_archive_omit as om

    tid, path, repo, bare = _network_clone_task(tmp_path)
    dangling = _local_work(repo)
    head = _run("rev-parse", "HEAD", cwd=repo)
    stash = _run("rev-parse", "refs/stash", cwd=repo)
    before = _tree_state(path)
    assert _gc() == 0
    assert not path.exists()

    archive = _archive(tid)
    manifest = sa.verify_complete_archive(archive)
    assert manifest["version"] == 2
    [record] = manifest["omitted_repos"]
    assert record["git_dir"] == "repo/.git" and record["reason"] == "remote"
    assert record["remotes"] == {"origin": FAKE_URL} and record["pack"] == "objects/0.pack"
    names = set(_members(tid))
    assert not [n for n in names if n.startswith("repo/.git/objects/") and _members(tid)[n][0].isfile()]
    assert "repo/.git/index" in names and "repo/.git/HEAD" in names
    # Clean tracked files are omitted (their blob is on the remote); edited/untracked ones are stored.
    assert set(manifest["omitted"]) == {"repo/big.txt"}
    assert {"repo/tracked.txt", "repo/bin/run.sh", "repo/staged.txt", "repo/new.txt"} <= names
    meta = json.loads((archive / "repo" / "meta.json").read_text())
    assert meta["objects_omitted"] == "remote" and meta["objects_restore"] == om.RESTORE_STEPS
    assert "commits.bundle" in meta["files"]

    target = tmp_path / "restored"
    om.restore(archive, target, url_map={FAKE_URL: str(bare)})
    assert _tree_state(target) == before
    restored = target / "repo"
    assert _run("rev-parse", "HEAD", cwd=restored) == head
    assert _run("rev-parse", "refs/stash", cwd=restored) == stash
    assert _run("cat-file", "-p", dangling, cwd=restored) == "dangling blob"
    assert _run("show", ":staged.txt", cwd=restored) == "staged only"
    assert _run("show", "stash@{0}:stashed.txt", cwd=restored) == "stash me"
    _run("fsck", "--connectivity-only", "--no-dangling", cwd=restored)
    status = {line.strip() for line in _run("status", "--porcelain", cwd=restored).splitlines()}
    assert status == {"AM staged.txt", "M bin/run.sh", "?? new.txt"}


def test_omitted_archive_is_under_a_tenth_of_a_full_clone(board, tmp_path):
    tid, path, _repo, _bare = _network_clone_task(tmp_path)
    source = sum(p.lstat().st_size for p in path.rglob("*") if p.is_file())
    assert _gc() == 0
    stored = sum(p.stat().st_size for p in _archive(tid).rglob("*") if p.is_file())
    assert stored < source / 10, (stored, source)


def test_local_path_remote_keeps_every_object(board, remote):
    from hermes_cli import kanban_scratch_archive as sa
    tid, _path = _task(remote=remote)
    assert _gc() == 0
    manifest = sa.verify_complete_archive(_archive(tid))
    assert manifest["version"] == 1 and "omitted" not in manifest
    objects = [n for n, (m, _) in _members(tid).items() if n.startswith("repo/.git/objects/") and m.isfile()]
    assert objects
    assert "objects_omitted" not in json.loads((_archive(tid) / "repo" / "meta.json").read_text())


def test_object_written_after_the_plan_keeps_workspace(board, tmp_path, monkeypatch):
    from hermes_cli import kanban_scratch_archive_omit as om
    tid, _path, repo, _bare = _network_clone_task(tmp_path)
    real = om.write_supplement

    def racing(*args, **kwargs):
        out = real(*args, **kwargs)
        subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=b"late object\n", check=True,
                       capture_output=True)
        return out

    monkeypatch.setattr(om, "write_supplement", racing)
    assert _gc() == 0
    assert (repo / ".git").is_dir()
    parent = _archive(tid).parent
    assert not parent.exists() or list(parent.iterdir()) == []


def test_clean_file_rewritten_after_hashing_keeps_workspace(board, tmp_path, monkeypatch):
    from hermes_cli import kanban_scratch_archive_omit as om
    tid, _path, repo, _bare = _network_clone_task(tmp_path)
    real = om.Omitter.__call__

    def racing(self, dfd, name, rel, st):
        out = real(self, dfd, name, rel, st)
        if rel == "repo/big.txt":
            (repo / "big.txt").write_text("local rewrite\n")
        return out

    monkeypatch.setattr(om.Omitter, "__call__", racing)
    assert _gc() == 0
    assert (repo / "big.txt").read_text() == "local rewrite\n"
    assert not _archive(tid).exists()


@pytest.mark.parametrize("tamper", ["pack_bytes", "omitted_oid", "no_remote_refs"])
def test_verify_rejects_tampered_omission_records(board, tmp_path, tamper):
    from hermes_cli import kanban_scratch_archive as sa
    tid, _path, repo, _bare = _network_clone_task(tmp_path)
    _local_work(repo)
    assert _gc() == 0
    base = _archive(tid) / PAYLOAD
    manifest = json.loads((base / "manifest.json").read_text())
    if tamper == "pack_bytes":
        pack = base / "objects" / "0.pack"
        pack.write_bytes(pack.read_bytes()[:-4] + b"XXXX")
    elif tamper == "omitted_oid":
        manifest["omitted"]["repo/big.txt"]["git_oid"] = "nothex"
    else:
        manifest["omitted_repos"][0]["remote_refs"] = {}
    (base / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(sa.ArchiveRefused):
        sa.verify_complete_archive(_archive(tid))


def test_restore_fails_loudly_when_the_remote_lacks_the_objects(board, tmp_path):
    from hermes_cli import kanban_scratch_archive_omit as om
    tid, _path, _repo, _bare = _network_clone_task(tmp_path)
    assert _gc() == 0
    empty = tmp_path / "empty.git"
    _run("init", "-q", "--bare", str(empty))
    with pytest.raises(Exception):
        om.restore(_archive(tid), tmp_path / "restored", url_map={FAKE_URL: str(empty)})


@pytest.mark.parametrize("url,network", [
    ("https://github.com/o/r.git", True), ("ssh://git@host/r", True), ("git@github.com:o/r.git", True),
    ("/srv/repo.git", False), ("../repo", False), ("file:///srv/r.git", False), ("C:/r", False), ("repo", False),
])
def test_only_network_remotes_count_as_holding_objects(url, network):
    from hermes_cli import kanban_scratch_archive_omit as om
    assert om._network_url(url) is network


def test_blobless_clone_omits_promised_objects_and_restores_lazily(board, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa
    from hermes_cli import kanban_scratch_archive_omit as om

    _tid0, _p0, _r0, bare = _network_clone_task(tmp_path)  # builds the fixture remote
    _run("config", "uploadpack.allowFilter", "true", cwd=bare)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="blobless clone")
        path = kb.workspaces_root() / tid
        path.mkdir(parents=True)
        _run("clone", "-q", "--filter=blob:none", f"file://{bare}", str(path / "repo"))
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done', workspace_kind='scratch', workspace_path=?, "
                         "completed_at=?, created_at=? WHERE id=?", (str(path), OLD, OLD, tid))
    repo = path / "repo"
    _run("remote", "set-url", "origin", FAKE_URL, cwd=repo)
    dangling = _local_work(repo)
    before = _tree_state(path)
    head = _run("rev-parse", "HEAD", cwd=repo)
    assert _gc() == 0
    assert not path.exists()
    manifest = sa.verify_complete_archive(_archive(tid))
    assert manifest["version"] == 2 and "repo/big.txt" in manifest["omitted"]
    target = tmp_path / "restored-blobless"
    om.restore(_archive(tid), target, url_map={FAKE_URL: f"file://{bare}"})
    assert _tree_state(target) == before
    restored = target / "repo"
    assert _run("rev-parse", "HEAD", cwd=restored) == head
    assert _run("cat-file", "-p", dangling, cwd=restored) == "dangling blob"
    assert _run("config", "remote.origin.promisor", cwd=restored) == "true"


def test_boundary_blobs_cover_unpushed_head(board, tmp_path):
    # HEAD is an unpushed commit: its unchanged files still match the pushed parent's blobs.
    tid, _path, repo, _bare = _network_clone_task(tmp_path)
    (repo / "tracked.txt").write_text("changed\n")
    _run("commit", "-q", "-am", "unpushed", cwd=repo)
    assert _gc() == 0
    from hermes_cli import kanban_scratch_archive as sa
    manifest = sa.verify_complete_archive(_archive(tid))
    assert set(manifest["omitted"]) == {"repo/big.txt", "repo/bin/run.sh"}


def test_tip_the_remote_deleted_and_gcd_is_archived_not_omitted(board, tmp_path):
    # Review repro: a remote-tracking ref whose branch the remote dropped (and gc'd) proves nothing.
    from hermes_cli import kanban_scratch_archive as sa
    from hermes_cli import kanban_scratch_archive_omit as om
    tid, _path, repo, bare = _network_clone_task(tmp_path)
    _run("checkout", "-q", "-b", "feature", cwd=repo)
    (repo / "feature.txt").write_text("only on the deleted branch\n")
    _run("add", "feature.txt", cwd=repo)
    _run("commit", "-q", "-m", "feature", cwd=repo)
    feature = _run("rev-parse", "HEAD", cwd=repo)
    _run("push", "-q", str(bare), "feature:feature", cwd=repo)
    _run("update-ref", "refs/remotes/origin/feature", feature, cwd=repo)
    _run("checkout", "-q", "main" if _run("branch", "--list", "main", cwd=repo) else "master", cwd=repo)
    _run("branch", "-q", "-D", "feature", cwd=repo)
    _run("branch", "-q", "-D", "feature", cwd=bare)
    _run("gc", "-q", "--prune=now", cwd=bare)
    assert _gc() == 0
    archive = _archive(tid)
    manifest = sa.verify_complete_archive(archive)
    [record] = manifest["omitted_repos"]
    assert "refs/remotes/origin/feature" not in record["remote_refs"]
    assert record["object_count"] >= 3  # the feature commit, its tree and feature.txt's blob
    target = tmp_path / "restored"
    om.restore(archive, target, url_map={FAKE_URL: str(bare)})
    assert _run("cat-file", "-p", f"{feature}:feature.txt", cwd=target / "repo") == "only on the deleted branch"


def test_tip_behind_a_moved_remote_branch_still_counts_as_held(board, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa
    from hermes_cli import kanban_scratch_archive_omit as om
    tid, _path, repo, bare = _network_clone_task(tmp_path)
    branch = _run("symbolic-ref", "--short", "HEAD", cwd=bare)
    stale = _run("rev-parse", f"refs/remotes/origin/{branch}", cwd=repo)
    other = tmp_path / "other"
    _run("clone", "-q", str(bare), str(other))
    (other / "tracked.txt").write_text("moved on\n")
    _run("commit", "-q", "-am", "remote moves on", cwd=other)
    _run("push", "-q", "origin", branch, cwd=other)
    assert _gc() == 0
    manifest = sa.verify_complete_archive(_archive(tid))
    [record] = manifest["omitted_repos"]
    assert record["remote_refs"][f"refs/remotes/origin/{branch}"] == stale
    assert record["object_count"] == 0 and "repo/big.txt" in manifest["omitted"]
    target = tmp_path / "restored"
    om.restore(_archive(tid), target, url_map={FAKE_URL: str(bare)})
    assert _run("rev-parse", "HEAD", cwd=target / "repo") == stale


def test_unreachable_remote_keeps_every_object(board, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa
    from hermes_cli import kanban_scratch_archive_omit as om
    tid, _path, _repo, _bare = _network_clone_task(tmp_path)
    om._PROBE_URL_MAP[FAKE_URL] = str(tmp_path / "no-such-remote.git")
    assert _gc() == 0
    manifest = sa.verify_complete_archive(_archive(tid))
    assert manifest["version"] == 1 and "omitted" not in manifest


def test_restore_writes_omitted_files_into_read_only_directories(board, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa
    from hermes_cli import kanban_scratch_archive_omit as om
    tid, path, repo, bare = _network_clone_task(tmp_path)
    (repo / "bin").chmod(0o555)
    try:
        before = _tree_state(path)
        bin_mtime = (repo / "bin").stat().st_mtime_ns
        assert _gc() == 0
    finally:
        if (repo / "bin").exists():
            (repo / "bin").chmod(0o755)
    manifest = sa.verify_complete_archive(_archive(tid))
    assert "repo/bin/run.sh" in manifest["omitted"]
    target = tmp_path / "restored"
    om.restore(_archive(tid), target, url_map={FAKE_URL: str(bare)})
    try:
        assert _tree_state(target) == before
        assert (target / "repo" / "bin").stat().st_mtime_ns // 10**9 == bin_mtime // 10**9
    finally:
        (target / "repo" / "bin").chmod(0o755)


def test_remote_probe_ignores_the_scratch_repos_own_config(board, tmp_path):
    # The probe may use the user's credential helpers, but never a helper or URL rewrite the scratch repo planted.
    from hermes_cli import kanban_scratch_archive as sa
    tid, _path, repo, _bare = _network_clone_task(tmp_path)
    marker = tmp_path / "helper-ran"
    _run("config", f"url.{tmp_path / 'nowhere.git'}.insteadOf", FAKE_URL, cwd=repo)
    _run("config", "credential.helper", f"!touch {marker}; false", cwd=repo)
    assert _gc() == 0
    manifest = sa.verify_complete_archive(_archive(tid))
    assert manifest["version"] == 2 and not marker.exists()


def _tracked_side_branch(tmp_path, repo, bare, *, local_branch=False):
    """Push a `side` branch to the remote from another clone and fetch it as refs/remotes/origin/side."""
    other = tmp_path / "side-pusher"
    _run("clone", "-q", str(bare), str(other))
    _run("checkout", "-q", "-b", "side", cwd=other)
    (other / "side.txt").write_text("only on side\n")
    _run("add", "side.txt", cwd=other)
    _run("commit", "-q", "-m", "side", cwd=other)
    _run("push", "-q", "origin", "side", cwd=other)
    side = _run("rev-parse", "HEAD", cwd=other)
    _run("fetch", "-q", str(bare), "+refs/heads/side:refs/remotes/origin/side", cwd=repo)
    if local_branch:
        _run("branch", "-q", "side", "refs/remotes/origin/side", cwd=repo)
    return side


def _delete_on_remote(bare, branch):
    _run("branch", "-q", "-D", branch, cwd=bare)
    _run("reflog", "expire", "--expire=now", "--all", cwd=bare)
    _run("gc", "-q", "--prune=now", cwd=bare)


def test_restore_drops_a_tracked_branch_the_remote_deleted_after_archiving(board, tmp_path):
    # Review nit on PR #2354: one tracked branch deleted+gc'd on the remote aborted the whole restore.
    from hermes_cli import kanban_scratch_archive as sa
    from hermes_cli import kanban_scratch_archive_omit as om
    tid, path, repo, bare = _network_clone_task(tmp_path)
    side = _tracked_side_branch(tmp_path, repo, bare)
    _local_work(repo)
    head = _run("rev-parse", "HEAD", cwd=repo)
    stash = _run("rev-parse", "refs/stash", cwd=repo)
    before = _tree_state(path)
    assert _gc() == 0
    archive = _archive(tid)
    [record] = sa.verify_complete_archive(archive)["omitted_repos"]
    assert record["remote_refs"]["refs/remotes/origin/side"] == side
    _delete_on_remote(bare, "side")

    target = tmp_path / "restored"
    manifest = om.restore(archive, target, url_map={FAKE_URL: str(bare)})
    assert manifest["dropped_remote_refs"] == {"repo/.git": {"refs/remotes/origin/side": side}}
    restored = target / "repo"
    assert _run("rev-parse", "HEAD", cwd=restored) == head
    assert _run("rev-parse", "refs/stash", cwd=restored) == stash
    assert not _run("for-each-ref", "refs/remotes/origin/side", cwd=restored)
    _run("fsck", "--connectivity-only", "--no-dangling", cwd=restored)
    before.pop("repo/.git/refs/remotes/origin/side", None)
    after = _tree_state(target)
    after.pop("repo/.git/packed-refs", None)
    before.pop("repo/.git/packed-refs", None)
    assert {k: v for k, v in after.items() if "/logs/" not in k} == {
        k: v for k, v in before.items() if "/logs/" not in k}


def test_restore_still_refuses_when_a_local_branch_needs_the_deleted_tip(board, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa
    from hermes_cli import kanban_scratch_archive_omit as om
    tid, _path, repo, bare = _network_clone_task(tmp_path)
    _tracked_side_branch(tmp_path, repo, bare, local_branch=True)
    assert _gc() == 0
    _delete_on_remote(bare, "side")
    with pytest.raises(sa.ArchiveRefused, match="refs/heads/side"):
        om.restore(_archive(tid), tmp_path / "restored", url_map={FAKE_URL: str(bare)})


def test_restore_failure_that_is_not_a_missing_object_still_raises(board, tmp_path, monkeypatch):
    # Only "the remote no longer has this oid" drops a ref; a network/auth failure keeps refusing.
    from hermes_cli import kanban_scratch_archive as sa
    from hermes_cli import kanban_scratch_archive_omit as om
    tid, _path, repo, bare = _network_clone_task(tmp_path)
    _tracked_side_branch(tmp_path, repo, bare)
    assert _gc() == 0
    real = om._run_git

    def flaky(repo_arg, env, *args):
        if "fetch" in args:
            raise sa.ArchiveRefused("git fetch failed: fatal: unable to access: Could not resolve host")
        return real(repo_arg, env, *args)

    monkeypatch.setattr(om, "_run_git", flaky)
    _run("update-ref", "-d", "refs/heads/side", cwd=bare)  # forces a fetch of the side tip
    with pytest.raises(sa.ArchiveRefused, match="Could not resolve host"):
        om.restore(_archive(tid), tmp_path / "restored", url_map={FAKE_URL: str(bare)})
