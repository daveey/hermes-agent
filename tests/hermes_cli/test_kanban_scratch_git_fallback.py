"""Unreadable nested Git metadata must not prevent byte-preserving scratch GC."""
import json
import subprocess
import tarfile

import pytest

from hermes_cli import kanban_scratch_archive as sa
from hermes_cli import kanban_worktree_gc as gc
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_scratch_gc_archive as helpers

board = helpers.board
remote = helpers.remote

FAILURES = [
    ("rev-parse", "fatal: multi-pack-index version 51 not recognized"),
    ("diff", "fatal: unable to read c6d5e1cc43a100ce951cd81e1333c30068760ebd"),
    ("ls-files", "warning: lazy fetching disabled; some objects may not be available\n"
     "fatal: could not fetch 8b2b70395ecd6ebcedfaac2e744a2d27f893de7f from promisor remote"),
    ("rev-parse", "fatal: not a git repository (or any of the parent directories): .git"),
    ("for-each-ref", "error: unable to normalize alternate object path: /missing/objects"),
]


def inject_git_failure(monkeypatch, repo, command, error):
    run = subprocess.run
    hits = []

    def fail(argv, *args, **kwargs):
        if argv[:3] == ["git", "-C", str(repo)] and argv[3] == command:
            hits.append(argv)
            return subprocess.CompletedProcess(argv, 128, b"", error.encode())
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(sa.subprocess, "run", fail)
    return hits


def assert_bytes_recovered(tid, before):
    archive = helpers._archive(tid)
    manifest = sa.verify_complete_archive(archive)
    assert manifest["version"] == 1  # no objects or clean worktree files omitted
    with tarfile.open(archive / sa.PAYLOAD_DIR / "tree.tar.gz", "r:gz") as tar:
        actual = {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}
    assert actual == before
    return json.loads((archive / sa.PAYLOAD_DIR / "git-fallback.json").read_text())


@pytest.mark.parametrize("command,error", FAILURES)
def test_git_128_archives_every_byte_then_gc_removes(board, remote, monkeypatch, command, error):
    tid, path = helpers._task(remote=remote)
    repo = path / "repo"
    (repo / "tracked.txt").write_text("unpublished edit\n")
    (repo / ".gitignore").write_text("ignored\n")
    (repo / "ignored").write_bytes(b"ignored evidence\x00")
    before = {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    hits = inject_git_failure(monkeypatch, repo, command, error)
    with kbc.connect_closing() as conn:
        assert gc.gc_task_scratch(conn, older_than_days=7) == 1
    assert hits and not path.exists()
    fallback = assert_bytes_recovered(tid, before)
    assert error in fallback["reason"]
    assert fallback["mode"] == "complete-filesystem"


@pytest.mark.parametrize("kind", ["broken", "midx", "missing-blob", "alternate"])
def test_real_unreadable_nested_repo(board, remote, kind):
    tid, path = helpers._task(remote=remote)
    repo = path / "repo"
    gitdir = repo / ".git"
    if kind == "broken":
        (gitdir / "HEAD").unlink()
    elif kind == "midx":
        (gitdir / "objects" / "pack" / "multi-pack-index").write_bytes(b"MIDX\x33\x01\x00\x00" + b"\x00" * 24)
    elif kind == "missing-blob":
        oid = helpers._run("rev-parse", "HEAD:tracked.txt", cwd=repo)
        (gitdir / "objects" / oid[:2] / oid[2:]).unlink()
        (repo / "tracked.txt").write_text("unpublished change\n")
    else:
        helpers._run("clone", "-q", "--bare", "--shared", str(remote), str(path / "bare.git"))
        (path / "bare.git" / "objects" / "info" / "alternates").write_text("/missing/objects\n")
    before = {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    with kbc.connect_closing() as conn:
        assert gc.gc_task_scratch(conn, older_than_days=7) == 1
    assert not path.exists()
    fallback = assert_bytes_recovered(tid, before)
    assert "rc 128" in fallback["reason"]


def test_discards_earlier_omissions_and_partial_git_outputs(board, remote, monkeypatch):
    tid, path = helpers._task(remote=remote)
    repo = path / "repo"
    before = {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}

    def prepared(workspace, repos, payload):
        (payload / "unused.pack").write_bytes(b"partial supplement")
        return [{"invalid_if_used": True}], {repo: {"invalid_if_used": True}}

    def fail_after_write(repo, dest, **kwargs):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "tracked.diff").write_bytes(b"incomplete diff")
        raise sa.GitInspectionFailed("git diff failed (rc 128)")

    monkeypatch.setattr(sa, "_omission_plans", prepared)
    monkeypatch.setattr(sa, "archive_repo", fail_after_write)
    with kbc.connect_closing() as conn:
        assert gc.gc_task_scratch(conn, older_than_days=7) == 1
    assert_bytes_recovered(tid, before)
    assert not (helpers._archive(tid) / "repo").exists()
    assert not (helpers._archive(tid) / sa.PAYLOAD_DIR / "unused.pack").exists()


@pytest.mark.parametrize("seam", ["_write_payload", "verify_complete_archive", "_fsync_tree", "check_snapshot"])
def test_fallback_keeps_all_payload_and_detach_guards(board, remote, monkeypatch, seam):
    tid, path = helpers._task(remote=remote)
    inject_git_failure(monkeypatch, path / "repo", "rev-parse", "broken repo")

    def refuse(*args, **kwargs):
        raise sa.ArchiveRefused("guard proof")

    monkeypatch.setattr(sa, seam, refuse)
    with kbc.connect_closing() as conn:
        assert gc.gc_task_scratch(conn, older_than_days=7) == 0
    assert (path / "repo" / "tracked.txt").read_text() == "one\n"
    assert not [kind for kind, _ in helpers._events(tid) if kind == "workspace_archived"]


def test_external_gitdir_remains_fail_closed(board, remote, monkeypatch, caplog):
    _, path = helpers._task(remote=remote)
    helpers._run("worktree", "add", "-q", "-b", "outside", str(path / "linked"), cwd=remote)
    inject_git_failure(monkeypatch, path / "repo", "rev-parse", "broken repo")
    with kbc.connect_closing() as conn:
        assert gc.gc_task_scratch(conn, older_than_days=7) == 0
    assert path.exists() and "external_gitdir" in caplog.text


@pytest.mark.parametrize("command", ["bundle", "index-pack"])
def test_archive_write_or_verification_errors_are_not_inspection_failures(board, remote, monkeypatch, command):
    inject_git_failure(monkeypatch, remote, command, "archive failed")
    with pytest.raises(sa.ArchiveRefused) as caught:
        sa._git(remote, sa._env(remote), command, "verify")
    assert not isinstance(caught.value, sa.GitInspectionFailed)


def test_disabled_retention_is_logged_without_inspecting_git(board, remote, monkeypatch, caplog):
    _, path = helpers._task(remote=remote)
    hits = inject_git_failure(monkeypatch, path / "repo", "rev-parse", "broken repo")
    with kbc.connect_closing() as conn:
        assert gc.gc_task_scratch(conn, older_than_days=0) == 0
    assert path.exists() and not hits
    assert "reason=retention_disabled" in caplog.text
