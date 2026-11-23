"""Scratch archive of partial (blobless) clones never lazily fetches. [daveey]

A blobless clone keeps HEAD's blobs on its promisor remote until checkout needs
them. ``git diff --binary HEAD`` lazily fetched every missing base blob, so on a
worker with no GitHub credential each such tree (e.g. a ``--no-checkout`` clone,
whose whole tree reads as staged deletions) was refused forever and never
reclaimed. The archive now records those paths as remote-base entries, keeps the
worktree side of any that still exist, and diffs only paths whose base is local.
"""
import json
import os
import shutil
import subprocess
import tarfile

import pytest

from tests.hermes_cli import test_kanban_scratch_gc_archive as base

board = base.board
_run = base._run
_task = base._task
_gc = base._gc
_archive = base._archive


@pytest.fixture
def promisor(tmp_path):
    """Bare remote serving filters: branch ``old`` = c1, ``main`` = c2 (new blobs)."""
    src = tmp_path / "pseed"
    src.mkdir()
    _run("init", "-q", cwd=src)
    (src / "tracked.txt").write_text("one\n")
    (src / "same.txt").write_text("same\n")
    _run("add", ".", cwd=src)
    _run("commit", "-q", "-m", "c1", cwd=src)
    _run("branch", "old", cwd=src)
    (src / "tracked.txt").write_text("two\n")
    (src / "other.txt").write_text("other\n")
    _run("add", ".", cwd=src)
    _run("commit", "-q", "-m", "c2", cwd=src)
    bare = tmp_path / "promisor.git"
    _run("clone", "-q", "--bare", str(src), str(bare))
    _run("config", "uploadpack.allowFilter", "true", cwd=bare)
    return bare


def _clone(remote, dest, *extra):
    _run("clone", "-q", "--filter=blob:none", *extra, f"file://{remote}", str(dest))
    assert _run("config", "--get", "remote.origin.promisor", cwd=dest) == "true"


def _missing(repo):
    walk = subprocess.run(["git", "rev-list", "--objects", "--missing=print", "--no-walk", "HEAD"],
                          cwd=repo, capture_output=True, text=True, check=True,
                          env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})
    return {line[1:] for line in walk.stdout.splitlines() if line.startswith("?")}


def test_no_checkout_blobless_clone_is_archived_without_credentials(board, promisor):
    tid, path = _task()
    _clone(promisor, path / "repo", "--no-checkout")
    assert _missing(path / "repo")  # every base blob lives on the promisor only
    shutil.move(promisor, promisor.with_name("gone.git"))  # lazy fetch now fails, like no creds
    assert _gc() == 0
    assert not path.exists()
    dest = _archive(tid) / "repo"
    meta = json.loads((dest / "meta.json").read_text())
    assert not (dest / "tracked.diff").exists()
    assert meta["remote_base_count"] == 3 and "remote_base.json" in meta["files"]
    entries = json.loads((dest / "remote_base.json").read_text())["entries"]
    assert sorted((e["status"], e["path"]) for e in entries) == [
        ("D", "other.txt"), ("D", "same.txt"), ("D", "tracked.txt")]
    assert not (dest / "remote_base.tar").exists()


def test_missing_base_keeps_worktree_side_diffs_the_rest_and_never_fetches(board, promisor, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa

    repo = tmp_path / "w" / "repo"
    _clone(promisor, repo, "-b", "old")
    _run("reset", "-q", "--soft", "origin/main", cwd=repo)  # HEAD's tracked/other blobs: remote only
    (repo / "same.txt").write_text("same\nlocal edit\n")
    before = _missing(repo)
    assert len(before) == 2
    dest = tmp_path / "out"
    meta = sa.archive_repo(repo, dest, task_id="t_x", completed_at=None)
    assert _missing(repo) == before  # nothing was fetched although the remote is reachable
    diff = (dest / "tracked.diff").read_text()
    assert "+local edit" in diff and "tracked.txt" not in diff and "other.txt" not in diff
    entries = {e["path"]: e for e in json.loads((dest / "remote_base.json").read_text())["entries"]}
    assert {p: e["status"] for p, e in entries.items()} == {"tracked.txt": "M", "other.txt": "D"}
    assert {e["old_oid"] for e in entries.values()} == before
    with tarfile.open(dest / "remote_base.tar") as tar:
        assert tar.getnames() == ["tracked.txt"]
        assert tar.extractfile("tracked.txt").read() == b"one\n"  # the local side survives
    assert meta["remote_base_count"] == 2
    assert {"tracked.diff", "remote_base.json", "remote_base.tar"} <= set(meta["files"])


def test_kept_paths_diff_in_batches_with_literal_pathspecs(board, promisor, tmp_path, monkeypatch):
    import functools

    from hermes_cli import kanban_scratch_archive as sa

    repo = tmp_path / "w" / "repo"
    _clone(promisor, repo, "-b", "old")
    (repo / "s*.txt").write_text("glob-looking name\n")
    _run("add", "s*.txt", cwd=repo)
    _run("reset", "-q", "--soft", "origin/main", cwd=repo)
    (repo / "same.txt").write_text("same\nlocal edit\n")
    monkeypatch.setattr(sa, "_batches", functools.partial(sa._batches, limit=1))  # one path per call
    sa.archive_repo(repo, tmp_path / "out", task_id="t_x", completed_at=None)
    diff = (tmp_path / "out" / "tracked.diff").read_text()
    assert diff.count("diff --git") == 2  # same.txt once, s*.txt once (literal, not a glob)
    assert "+local edit" in diff and "+glob-looking name" in diff
    assert sa._batches(["a", "bb", "c"], limit=5) == [["a", "bb"], ["c"]]


def test_complete_blobless_clone_archives_like_a_full_one(board, promisor, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa

    repo = tmp_path / "w" / "repo"
    _clone(promisor, repo)
    (repo / "tracked.txt").write_text("two\nlocal edit\n")
    dest = tmp_path / "out"
    meta = sa.archive_repo(repo, dest, task_id="t_x", completed_at=None)
    assert "+local edit" in (dest / "tracked.diff").read_text()
    assert "remote_base_count" not in meta
    assert not (dest / "remote_base.json").exists()


# "" is `promisor =`, which git reads as bool false; "No" checks case-insensitive bool parsing.
@pytest.mark.parametrize("promisor_value", [None, "false", "", "No", "0"])
def test_missing_object_in_a_full_clone_still_refuses(board, tmp_path, promisor_value):
    from hermes_cli import kanban_scratch_archive as sa

    repo = tmp_path / "w" / "repo"
    repo.mkdir(parents=True)
    _run("init", "-q", cwd=repo)
    if promisor_value is not None:  # a remote explicitly NOT a promisor vouches for nothing
        _run("config", "remote.origin.promisor", promisor_value, cwd=repo)
        assert not sa._is_partial_clone(repo, sa._env(repo))
    (repo / "f.txt").write_text("base\n")
    _run("add", ".", cwd=repo)
    _run("commit", "-q", "-m", "c", cwd=repo)
    _run("update-ref", "refs/remotes/origin/main", "HEAD", cwd=repo)  # pushed: no bundle step
    blob = _run("rev-parse", "HEAD:f.txt", cwd=repo)
    loose = repo / ".git" / "objects" / blob[:2] / blob[2:]
    loose.chmod(0o600)
    loose.unlink()  # corruption, not a promise: nothing vouches for it remotely
    (repo / "f.txt").unlink()  # deleted: `diff --raw` still lists it, only the patch needs the blob
    with pytest.raises(sa.ArchiveRefused, match="unable to read"):
        sa.archive_repo(repo, tmp_path / "out", task_id="t_x", completed_at=None)


def test_unborn_partial_clone_archives_its_files(board, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa

    repo = tmp_path / "w" / "repo"
    repo.mkdir(parents=True)
    _run("init", "-q", cwd=repo)
    _run("config", "remote.origin.promisor", "true", cwd=repo)  # e.g. a filtered clone of an empty repo
    (repo / "new.txt").write_text("fresh\n")
    _run("add", "new.txt", cwd=repo)  # staged against the empty tree: a raw change, but no HEAD to walk
    meta = sa.archive_repo(repo, tmp_path / "out", task_id="t_x", completed_at=None)
    assert meta["head"] is None and "+fresh" in (tmp_path / "out" / "tracked.diff").read_text()
    assert "remote_base_count" not in meta


@pytest.mark.parametrize("key,value,expected", [
    ("remote.origin.promisor", "true", True),
    ("remote.origin.promisor", "yes", True),
    ("remote.origin.promisor", "1", True),
    ("extensions.partialClone", "origin", True),  # names the promisor remote: not a bool
    ("extensions.partialClone", "", False),
])
def test_is_partial_clone_reads_git_bool_semantics(board, tmp_path, key, value, expected):
    from hermes_cli import kanban_scratch_archive as sa

    repo = tmp_path / "r"
    repo.mkdir()
    _run("init", "-q", cwd=repo)
    assert not sa._is_partial_clone(repo, sa._env(repo))
    _run("config", key, value, cwd=repo)
    assert sa._is_partial_clone(repo, sa._env(repo)) is expected


def test_unparseable_promisor_value_refuses(board, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa

    repo = tmp_path / "r"
    repo.mkdir()
    _run("init", "-q", cwd=repo)
    _run("config", "remote.origin.promisor", "maybe", cwd=repo)  # git itself rejects this as a bool
    with pytest.raises(sa.ArchiveRefused):
        sa._is_partial_clone(repo, sa._env(repo))


def test_archive_git_never_reaches_the_network(board, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa

    repo = tmp_path / "r"
    repo.mkdir()
    _run("init", "-q", cwd=repo)
    env = sa._env(repo)
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    count = int(env["GIT_CONFIG_COUNT"])
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
    assert pairs["protocol.allow"] == "never"
    probe = subprocess.run(["git", "-C", str(repo), "config", "--get", "protocol.allow"],
                           env=env, capture_output=True, text=True, check=True)
    assert probe.stdout.strip() == "never"


def _git_243(sa, monkeypatch, extra: str):
    """git 2.43's ``--exclude-promisor-objects`` walk: it also lists *extra* (see the test below)."""
    real = sa._git

    def git(repo, env, *args, **kwargs):
        proc = real(repo, env, *args, **kwargs)
        if args[:1] == ("rev-list",) and "--exclude-promisor-objects" in args and "--stdin" in args:
            proc = subprocess.CompletedProcess(proc.args, proc.returncode, proc.stdout + f"{extra}\n".encode(),
                                               proc.stderr)
        return proc
    monkeypatch.setattr(sa, "_git", git)


def test_head_a_promisor_walk_wrongly_lists_needs_no_bundle(board, promisor, tmp_path, monkeypatch):
    """worker-a2, 2026-11-23 (t_01a1fb485b17a490): refused 6 times with "Refusing to create empty bundle".

    That clone borrowed its history from the host checkout through ``objects/info/alternates``; the
    remote tip sat in its promisor pack too. git 2.43 drops such a tip from the ``--not`` side of an
    ``--exclude-promisor-objects`` walk, so 3348 commits of origin/main, HEAD among them, read as
    unpushed (git 2.55 on the hub does not, hence the injected walk). ``bundle create HEAD --not
    --remotes`` then had nothing to write and the tree was kept forever.
    """
    from hermes_cli import kanban_scratch_archive as sa

    repo = tmp_path / "w" / "repo"
    _clone(promisor, repo)
    head = _run("rev-parse", "HEAD", cwd=repo)
    _git_243(sa, monkeypatch, head)
    dest = tmp_path / "out"
    meta = sa.archive_repo(repo, dest, task_id="t_x", completed_at=None)
    assert not (dest / "commits.bundle").exists()
    assert meta["unpushed_refs"] == []


def test_local_commit_is_bundled_even_with_the_243_walk(board, promisor, tmp_path, monkeypatch):
    from hermes_cli import kanban_scratch_archive as sa

    repo = tmp_path / "w" / "repo"
    _clone(promisor, repo)
    pushed = _run("rev-parse", "HEAD", cwd=repo)
    (repo / "mine.txt").write_text("mine\n")
    _run("add", ".", cwd=repo)
    _run("commit", "-q", "-m", "mine", cwd=repo)
    _git_243(sa, monkeypatch, pushed)
    dest = tmp_path / "out"
    meta = sa.archive_repo(repo, dest, task_id="t_x", completed_at=None)
    assert meta["unpushed_refs"] == ["HEAD", "refs/heads/main"]
    heads = _run("bundle", "list-heads", str(dest / "commits.bundle"), cwd=repo)
    assert _run("rev-parse", "HEAD", cwd=repo) in heads


def test_a_truly_local_commit_in_a_partial_clone_is_still_bundled(board, promisor, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa

    repo = tmp_path / "w" / "repo"
    _clone(promisor, repo)
    (repo / "mine.txt").write_text("mine\n")
    _run("add", ".", cwd=repo)
    _run("commit", "-q", "-m", "mine", cwd=repo)
    dest = tmp_path / "out"
    sa.archive_repo(repo, dest, task_id="t_x", completed_at=None)
    heads = _run("bundle", "list-heads", str(dest / "commits.bundle"), cwd=repo)
    assert _run("rev-parse", "HEAD", cwd=repo) in heads


def test_failed_plain_walk_keeps_the_promisor_walk_set(board, promisor, tmp_path, monkeypatch):
    """A plain walk that exits 128 (e.g. a missing commit) changes nothing: the refusal path stays as before."""
    from hermes_cli import kanban_scratch_archive as sa

    repo = tmp_path / "w" / "repo"
    _clone(promisor, repo)
    head = _run("rev-parse", "HEAD", cwd=repo)
    _git_243(sa, monkeypatch, head)
    listed = sa._git

    def git(repo_, env, *args, **kwargs):
        if args[:1] == ("rev-list",) and "--stdin" in args and "--exclude-promisor-objects" not in args \
                and "--objects" not in args:
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: bad object")
        return listed(repo_, env, *args, **kwargs)
    monkeypatch.setattr(sa, "_git", git)
    with pytest.raises(sa.ArchiveRefused, match="empty bundle"):
        sa.archive_repo(repo, tmp_path / "out", task_id="t_x", completed_at=None)
