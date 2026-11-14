"""Scratch shapes the archive refused forever now archive, then get removed. [daveey]

worker-2 (2026-11-13): 54 aged trees / 37.8 GiB were kept because every retry of
the archive failed the same way: bundles of promisor-fetched tags ("unable to
read"), linked worktrees and submodules, bare repositories and fifos/sockets.
Each shape below must archive and be removed through the normal guarded path,
while objects that are really missing still refuse (``unrecoverable_objects``).
"""
import json
import os
import shutil
import socket
import stat
import subprocess
import tarfile

import pytest

from tests.hermes_cli import test_kanban_scratch_gc_archive as base

board = base.board
remote = base.remote
_run = base._run
_task = base._task
_gc = base._gc
_archive = base._archive
PAYLOAD = base.PAYLOAD


def _meta(tid, rel):
    return json.loads((_archive(tid) / rel / "meta.json").read_text())


def _bundle_heads(bundle, cwd):
    return {line.split()[1] for line in _run("bundle", "list-heads", str(bundle), cwd=cwd).splitlines()}


def _manifest(tid):
    return json.loads((_archive(tid) / PAYLOAD / "manifest.json").read_text())


def _tree(tid):
    with tarfile.open(_archive(tid) / PAYLOAD / "tree.tar.gz") as tar:
        return {m.name: m for m in tar.getmembers()}


# --- 1. unpushed detection ---------------------------------------------------------------

@pytest.fixture
def tagged_promisor(tmp_path):
    """Filter-serving bare remote: main = c1, c2; tag ``archive-2023`` on a side commit main lacks."""
    src = tmp_path / "tseed"
    src.mkdir()
    _run("init", "-q", cwd=src)
    (src / "a.txt").write_text("one\n")
    _run("add", ".", cwd=src)
    _run("commit", "-q", "-m", "c1", cwd=src)
    _run("checkout", "-q", "-b", "side", cwd=src)
    (src / "side.txt").write_text("first side version\n")
    _run("add", ".", cwd=src)
    _run("commit", "-q", "-m", "side 1", cwd=src)
    (src / "side.txt").write_text("tagged side version\n")
    _run("commit", "-q", "-am", "side 2", cwd=src)
    _run("tag", "-a", "-m", "old", "archive-2023", cwd=src)
    _run("checkout", "-q", "main", cwd=src)
    _run("branch", "-q", "-D", "side", cwd=src)
    (src / "a.txt").write_text("two\n")
    _run("commit", "-q", "-am", "c2", cwd=src)
    bare = tmp_path / "tagged.git"
    _run("clone", "-q", "--bare", str(src), str(bare))
    _run("config", "uploadpack.allowFilter", "true", cwd=bare)
    return bare


def _blobless(remote_path, dest):
    _run("clone", "-q", "--filter=blob:none", f"file://{remote_path}", str(dest))
    assert "refs/tags/archive-2023" in _run("for-each-ref", "--format=%(refname)", cwd=dest)


def test_promisor_fetched_tag_is_not_bundled_and_the_tree_is_removed(board, tagged_promisor, tmp_path):
    tid, path = _task()
    repo = path / "repo"
    _blobless(tagged_promisor, repo)
    (repo / "a.txt").write_text("local\n")
    _run("commit", "-q", "-am", "local work", cwd=repo)
    head = _run("rev-parse", "HEAD", cwd=repo)
    shutil.move(tagged_promisor, tagged_promisor.with_name("gone.git"))  # no lazy fetch can rescue it
    assert _gc() == 0
    assert not path.exists()
    meta = _meta(tid, "repo")
    assert meta["unpushed_refs"] == ["HEAD", "refs/heads/main"]  # the tag's tip is the remote's
    assert "promised_refs" not in meta
    check = tmp_path / "check"
    _run("init", "-q", str(check))
    bundle = _archive(tid) / "repo" / "commits.bundle"
    assert _bundle_heads(bundle, check) == {"HEAD", "refs/heads/main"}
    assert head in _run("bundle", "list-heads", str(bundle), cwd=check)


def test_local_clone_of_a_blobless_clone_leaves_promisor_tags_out(board, tagged_promisor, tmp_path):
    """The 10 GiB rv3/corerepo shape: a local clone of a blobless clone, no promisor config of its own."""
    seed = tmp_path / "partial"
    _blobless(tagged_promisor, seed)
    tid, path = _task()
    repo = path / "corerepo"
    _run("clone", "-q", str(seed), str(repo))
    promisor = subprocess.run(["git", "config", "--get-regexp", "promisor"], cwd=repo, capture_output=True, check=False)
    assert promisor.returncode == 1  # not a partial clone by config: nothing "promised"
    (repo / "a.txt").write_text("local\n")
    _run("commit", "-q", "-am", "local work", cwd=repo)
    assert _gc() == 0
    assert not path.exists()
    assert _meta(tid, "corerepo")["unpushed_refs"] == ["HEAD", "refs/heads/main"]


def test_clean_blobless_clone_with_only_promisor_tags_writes_meta_only(board, tagged_promisor):
    tid, path = _task()
    _blobless(tagged_promisor, path / "repo")
    assert _gc() == 0
    assert not path.exists()
    assert sorted(os.listdir(_archive(tid) / "repo")) == ["meta.json"]
    assert _meta(tid, "repo")["unpushed_refs"] == []


def test_ref_needing_promised_objects_is_left_to_the_payload(board, tagged_promisor, tmp_path):
    tid, path = _task()
    repo = path / "repo"
    _blobless(tagged_promisor, repo)
    _run("checkout", "-q", "-b", "feat", "archive-2023", cwd=repo)  # fetches the tag's tree blobs only
    (repo / "feat.txt").write_text("feature work\n")
    _run("add", "feat.txt", cwd=repo)
    _run("commit", "-q", "-m", "feature", cwd=repo)
    feat = _run("rev-parse", "HEAD", cwd=repo)
    _run("checkout", "-q", "main", cwd=repo)
    # feat's bundle would need "side 1"'s blob below the tag: promised by the remote, never local.
    shutil.move(tagged_promisor, tagged_promisor.with_name("gone.git"))
    assert _gc() == 0
    assert not path.exists()
    meta = _meta(tid, "repo")
    assert meta["promised_refs"] == ["refs/heads/feat"]
    assert meta["unpushed_refs"] == []
    # The feature commit itself is still archived: the payload carries the repo's local objects.
    restored = tmp_path / "restored"
    with tarfile.open(_archive(tid) / PAYLOAD / "tree.tar.gz", "r:gz") as tar:
        tar.extractall(restored, filter="tar")
    assert _run("cat-file", "-t", feat, cwd=restored / "repo") == "commit"


def test_really_missing_unpushed_object_refuses_as_unrecoverable(board, remote):
    from hermes_cli import kanban_scratch_archive as sa
    from hermes_cli import kanban_scratch_backoff as bo

    tid, path = _task(remote=remote)
    repo = path / "repo"
    (repo / "new.txt").write_text("written once, then lost\n")
    _run("add", "new.txt", cwd=repo)
    _run("commit", "-q", "-m", "unpushed", cwd=repo)
    blob = _run("rev-parse", "HEAD:new.txt", cwd=repo)
    loose = repo / ".git" / "objects" / blob[:2] / blob[2:]
    loose.chmod(0o600)
    loose.unlink()  # no promisor remote vouches for it: the work is gone
    with pytest.raises(sa.ArchiveRefused, match="^unrecoverable_objects: .* 1 missing object"):
        sa.archive_workspace(path, _archive(tid), task_id=tid, completed_at=base.OLD)
    assert _gc() == 0
    assert (repo / "new.txt").exists() and not _archive(tid).exists()
    assert bo.Refusals("default").entries[tid]["reason"].startswith("unrecoverable_objects:")


def test_select_unpushed_handles_nested_and_non_commit_tags(board, remote, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa
    from hermes_cli import kanban_scratch_archive_shapes as shapes

    repo = tmp_path / "r"
    _run("clone", "-q", str(remote), str(repo))
    (repo / "tracked.txt").write_text("two\n")
    _run("commit", "-q", "-am", "local", cwd=repo)
    _run("tag", "-a", "-m", "inner", "inner", cwd=repo)
    _run("tag", "-a", "-m", "outer", "outer", "inner", cwd=repo)
    _run("tag", "-a", "-m", "pushed", "pushed-outer", "origin/main", cwd=repo)
    _run("tag", "-a", "-m", "wrap", "pushed-nested", "pushed-outer", cwd=repo)
    _run("tag", "tree-tag", "HEAD^{tree}", cwd=repo)  # names no commit: nothing unpushed
    env = sa._env(repo)
    refs, extra = shapes.select_unpushed(repo, env, sa._git, _run("rev-parse", "HEAD", cwd=repo), partial=False)
    assert refs == ["HEAD", "refs/heads/main", "refs/tags/inner", "refs/tags/outer"]
    assert extra == {}


# --- 2. bare repositories ------------------------------------------------------------------

def test_bare_repo_is_bundled_with_bare_meta_and_removed(board, remote, tmp_path):
    tid, path = _task(remote=remote)
    _run("clone", "-q", "--bare", str(remote), str(path / "mirror.git"))
    tip = _run("rev-parse", "refs/heads/main", cwd=path / "mirror.git")
    assert _gc() == 0
    assert not path.exists()
    meta = _meta(tid, "mirror.git")
    assert meta["bare"] is True and meta["head"] == tip
    assert meta["unpushed_refs"] == ["HEAD", "refs/heads/main"]  # a bare clone has no refs/remotes
    assert "tracked.diff" not in meta["files"] and meta["untracked_count"] == 0
    check = tmp_path / "check"
    _run("init", "-q", str(check))
    bundle = _archive(tid) / "mirror.git" / "commits.bundle"
    _run("bundle", "verify", str(bundle), cwd=check)
    assert _bundle_heads(bundle, check) == {"HEAD", "refs/heads/main"}
    assert _meta(tid, "repo").get("bare") is None


def test_workspace_root_that_is_a_bare_repo(board, remote):
    tid, path = _task()
    (path / "notes.txt").unlink()
    path.rmdir()
    _run("clone", "-q", "--bare", str(remote), str(path))
    assert _gc() == 0
    assert not path.exists()
    assert _meta(tid, ".")["bare"] is True


def test_directory_that_only_looks_bare_refuses(board):
    from hermes_cli import kanban_scratch_archive as sa

    def build(p):
        fake = p / "fixture"
        (fake / "objects").mkdir(parents=True)
        (fake / "refs").mkdir()
        (fake / "HEAD").write_text("not a ref\n")

    tid, path = base._plain_task(build)
    with pytest.raises(sa.ArchiveRefused):
        sa.archive_workspace(path, _archive(tid), task_id=tid, completed_at=base.OLD)
    assert _gc() == 0 and (path / "fixture" / "HEAD").exists()


# --- 3. linked worktrees and submodules ----------------------------------------------------

def test_linked_worktree_with_gitdir_inside_is_archived_as_a_nested_repo(board, remote, tmp_path):
    tid, path = _task(remote=remote)
    _run("worktree", "add", "-q", "-b", "side", str(path / "wt"), cwd=path / "repo")
    (path / "wt" / "tracked.txt").write_text("side work\n")
    _run("commit", "-q", "-am", "side commit", cwd=path / "wt")
    (path / "wt" / "tracked.txt").write_text("side work\nuncommitted\n")
    assert _gc() == 0
    assert not path.exists()
    meta = _meta(tid, "wt")
    assert meta["gitdir_inside"] is True and meta["linked_gitdir"].endswith(".git/worktrees/wt")
    assert meta["branch"] == "side" and "refs/heads/side" in meta["unpushed_refs"]
    assert "+uncommitted" in (_archive(tid) / "wt" / "tracked.diff").read_text()
    check = tmp_path / "check"
    _run("clone", "-q", str(remote), str(check))
    _run("bundle", "verify", str(_archive(tid) / "wt" / "commits.bundle"), cwd=check)


def test_submodule_with_gitdir_inside_is_archived(board, remote, tmp_path):
    (tmp_path / "subseed").mkdir()
    sub_remote = base._seed_remote(tmp_path / "subseed")
    tid, path = _task(remote=remote)
    repo = path / "repo"
    _run("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_remote), "sub", cwd=repo)
    assert (repo / "sub" / ".git").is_file()
    (repo / "sub" / "tracked.txt").write_text("submodule edit\n")
    assert _gc() == 0
    assert not path.exists()
    meta = _meta(tid, "repo/sub")
    assert meta["gitdir_inside"] is True and meta["linked_gitdir"] == "../.git/modules/sub"
    assert "+submodule edit" in (_archive(tid) / "repo" / "sub" / "tracked.diff").read_text()


def _fingerprint(root):
    """Every entry's bytes and stat below *root* (the outside gitdir must not change at all)."""
    out = {}
    for current, dirs, files in os.walk(root):
        for name in dirs + files:
            full = os.path.join(current, name)
            st = os.lstat(full)
            data = None
            if stat.S_ISREG(st.st_mode):
                with open(full, "rb") as fh:
                    data = fh.read()
            out[os.path.relpath(full, root)] = (st.st_mode, st.st_size, st.st_mtime_ns, data)
    return out


def test_linked_worktree_with_gitdir_outside_records_diff_and_never_writes_there(board, remote):
    outside = board / "outside-repo"
    _run("clone", "-q", str(remote), str(outside))
    tid, path = _task()
    wt = path / "wt"
    _run("worktree", "add", "-q", "-b", "wt-branch", str(wt), cwd=outside)
    (wt / "tracked.txt").write_text("one\nworktree edit\n")
    (wt / "fresh.txt").write_text("untracked\n")
    (wt / "same.txt").write_text("x\n")
    _run("add", "same.txt", cwd=wt)
    # Stat-dirty but unchanged: a plain `git diff` would rewrite the outside index to refresh it.
    st = (wt / "same.txt").stat()
    os.utime(wt / "same.txt", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    before = _fingerprint(outside / ".git")
    assert _gc() == 0
    assert not path.exists()
    assert _fingerprint(outside / ".git") == before
    meta = _meta(tid, "wt")
    assert meta["gitdir_inside"] is False and meta["linked_gitdir"] == str(outside / ".git" / "worktrees" / "wt")
    assert meta["branch"] == "wt-branch" and meta["head"] == _run("rev-parse", "main", cwd=outside)
    assert meta["unpushed_refs"] == [] and "commits.bundle" not in meta["files"]
    diff = (_archive(tid) / "wt" / "tracked.diff").read_text()
    assert "+worktree edit" in diff and "+x" in diff
    with tarfile.open(_archive(tid) / "wt" / "untracked.tar") as tar:
        assert tar.getnames() == ["fresh.txt"]
    assert not [p for p in (_archive(tid) / "wt").iterdir() if p.name.startswith(".index-")]


@pytest.mark.parametrize("shape", ["missing", "empty", "inside_file", "inside_symlink"])
def test_gitlink_it_cannot_archive_still_refuses(board, shape):
    from hermes_cli import kanban_scratch_archive as sa

    def build(p):
        (p / "wt").mkdir()
        (p / "target-file").write_text("not a gitdir\n")
        (p / "real").mkdir()
        (p / "via-link").symlink_to("real")
        pointer = {"missing": f"gitdir: {p.parent / 'no-such-gitdir'}", "empty": "gitdir:   ",
                   "inside_file": "gitdir: ../target-file", "inside_symlink": "gitdir: ../via-link"}[shape]
        (p / "wt" / ".git").write_text(pointer + "\n")

    _tid, path = base._plain_task(build)
    with pytest.raises(sa.ArchiveRefused, match="linked worktree or submodule"):
        sa.find_repos(path)
    assert _gc() == 0 and (path / "wt" / ".git").is_file()


def test_dir_whose_gitdir_points_elsewhere_is_not_bare(board, remote, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa

    parent = tmp_path / "parent.git"
    _run("clone", "-q", "--bare", str(remote), str(parent))
    candidate = parent / "fixture"
    (candidate / "objects").mkdir(parents=True)
    (candidate / "refs").mkdir()
    (candidate / "HEAD").write_text("not a ref\n")
    assert _run("rev-parse", "--absolute-git-dir", cwd=candidate) == str(parent)
    with pytest.raises(sa.ArchiveRefused, match="looks like a bare repository but git resolves"):
        sa.archive_repo(candidate, tmp_path / "archive", task_id="fixture", completed_at=base.OLD, bare=True)


def test_archive_never_touches_the_repo_index(board, remote, tmp_path, monkeypatch):
    from hermes_cli import kanban_scratch_archive as sa

    outside = tmp_path / "outside"
    _run("clone", "-q", str(remote), str(outside))
    tid, path = _task()
    wt = path / "wt"
    _run("worktree", "add", "-q", "-b", "side", str(wt), cwd=outside)
    index = outside / ".git" / "worktrees" / "wt" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    real = sa._archive_worktree
    refreshed = []

    def refresh(repo, dest, env, head, files, extra):
        # Force an actual index write, independent of Git's optional refresh policy.
        # A diff implementation may refresh its index; only the private copy is writable.
        sa._git(repo, env, "update-index", "--force-write-index")
        refreshed.append(env.get("GIT_INDEX_FILE"))
        assert (index.read_bytes(), index.stat().st_mtime_ns) == before
        return real(repo, dest, env, head, files, extra)

    monkeypatch.setattr(sa, "_archive_worktree", refresh)
    sa.archive_workspace(path, _archive(tid), task_id=tid, completed_at=base.OLD)
    assert len(refreshed) == 1 and refreshed[0] != str(index)
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
    assert not list(_archive(tid).rglob(".index-*"))


def test_gitlink_changed_after_scan_refuses(board, remote, monkeypatch):
    from hermes_cli import kanban_scratch_archive as sa

    tid, path = _task(remote=remote)
    _run("worktree", "add", "-q", "-b", "side", str(path / "wt"), cwd=path / "repo")
    marker = path / "wt" / ".git"
    real = sa._write_payload
    changed = []

    def change_then_scan(*args, **kwargs):
        marker.write_text(marker.read_text() + "\n")  # still valid, but no longer the judged file
        changed.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(sa, "_write_payload", change_then_scan)
    with pytest.raises(sa.ArchiveRefused, match=r"a \.git file changed after it was judged"):
        sa.archive_workspace(path, _archive(tid), task_id=tid, completed_at=base.OLD)
    assert changed == [True] and marker.exists()
    assert not _archive(tid).exists()


def test_outside_gitdir_commits_are_not_bundled(board, remote, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa

    outside = tmp_path / "outside"
    _run("clone", "-q", str(remote), str(outside))
    tid, path = _task()
    wt = path / "wt"
    _run("worktree", "add", "-q", "-b", "side", str(wt), cwd=outside)
    (wt / "tracked.txt").write_text("private unpushed work\n")
    _run("commit", "-q", "-am", "local only", cwd=wt)
    head = _run("rev-parse", "HEAD", cwd=wt)
    assert head != _run("rev-parse", "origin/main", cwd=wt)
    before = _fingerprint(outside / ".git")
    sa.archive_workspace(path, _archive(tid), task_id=tid, completed_at=base.OLD)
    meta = _meta(tid, "wt")
    assert meta["head"] == head and meta["gitdir_inside"] is False
    assert meta["unpushed_refs"] == [] and "commits.bundle" not in meta["files"]
    assert not list(_archive(tid).rglob("commits.bundle"))
    assert _fingerprint(outside / ".git") == before


def test_bare_repo_contents_are_not_walked_as_workspace_files(board, remote):
    from hermes_cli import kanban_scratch_archive as sa

    tid, path = _task()
    bare = path / "mirror.git"
    _run("clone", "-q", "--bare", str(remote), str(bare))
    # Metadata may contain a file named .git; it is payload, not a nested worktree.
    marker = bare / "hooks" / ".git"
    marker.write_text("gitdir: /nonexistent/archive-test-metadata\n")
    sa.archive_workspace(path, _archive(tid), task_id=tid, completed_at=base.OLD)
    assert _meta(tid, "mirror.git")["bare"] is True
    with tarfile.open(_archive(tid) / PAYLOAD / "tree.tar.gz") as tar:
        member = tar.extractfile("mirror.git/hooks/.git")
        assert member is not None and member.read() == marker.read_bytes()
    assert not (_archive(tid) / "mirror.git" / "hooks" / "meta.json").exists()


# --- 4. fifos, sockets, devices ------------------------------------------------------------

def test_fifo_and_socket_are_recorded_without_content_and_removed(board, monkeypatch):
    from hermes_cli import kanban_scratch_archive as sa

    def build(p):
        os.mkfifo(p / "pipe")
        (p / "run").mkdir()
        monkeypatch.chdir(p / "run")  # AF_UNIX paths are short: bind relative to the directory
        sock = socket.socket(socket.AF_UNIX)
        sock.bind("agent.sock")
        sock.close()

    tid, path = base._plain_task(build)
    monkeypatch.chdir(board)
    assert _gc() == 0
    assert not path.exists()
    entries = _manifest(tid)["entries"]
    assert entries["pipe"]["type"] == "fifo" and entries["run/agent.sock"]["type"] == "socket"
    assert "sha256" not in entries["pipe"]
    members = _tree(tid)
    assert "pipe" not in members and "run/agent.sock" not in members and "run" in members
    sa.verify_complete_archive(_archive(tid))


def test_verify_still_requires_every_member_with_content(board):
    from hermes_cli import kanban_scratch_archive as sa

    tid, _ = base._plain_task(lambda p: os.mkfifo(p / "pipe"))
    assert _gc() == 0
    manifest_path = _archive(tid) / PAYLOAD / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["entries"]["ghost.txt"] = {"type": "file", "mode": 0o644, "size": 1, "sha256": "0" * 64}
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(sa.ArchiveRefused, match="missing members"):
        sa.verify_complete_archive(_archive(tid))


@pytest.mark.parametrize("mode", [stat.S_IFCHR, stat.S_IFBLK])
def test_device_node_is_still_refused(mode):
    from hermes_cli import kanban_scratch_archive as sa

    with pytest.raises(sa.ArchiveRefused, match="device node"):
        sa._kind(mode | 0o600)
    assert sa._kind(stat.S_IFIFO | 0o600) == "fifo" and sa._kind(stat.S_IFSOCK | 0o600) == "socket"
