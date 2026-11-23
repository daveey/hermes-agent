"""Inert local custody refs. Real temporary Git repositories, mocked signals only."""
from __future__ import annotations

import os
import subprocess
import sys
import signal
from pathlib import Path

import pytest

from hermes_cli import kanban_source_refs as refs

pytestmark = pytest.mark.platforms("posix")

STORE = "ab" * 16
OP = "cd" * 16
UNIT = "ef" * 16


def _env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        env.pop(key, None)
    env.update(GIT_AUTHOR_NAME="Custody Test", GIT_AUTHOR_EMAIL="custody@example.com",
               GIT_COMMITTER_NAME="Custody Test", GIT_COMMITTER_EMAIL="custody@example.com")
    return env


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True,
                          text=True, env=_env())


def _repo(path: Path, *, fmt: str = "sha1") -> Path:
    path.mkdir(parents=True)
    cmd = ["git", "init", str(path)]
    if fmt != "sha1":
        cmd[2:2] = ["--object-format", fmt]
    subprocess.run(cmd, check=True, capture_output=True, text=True, env=_env())
    _git(path, "config", "user.name", "Custody Test")
    _git(path, "config", "user.email", "custody@example.com")
    (path / "kept.txt").write_text("kept-bytes\n", encoding="utf-8")
    _git(path, "add", "kept.txt")
    _git(path, "commit", "-m", "keep")
    return path


def _oid(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.parametrize('packed', [False, True])
@pytest.mark.parametrize('operation', ['create', 'delete'])
def test_symbolic_custody_ref_keeps_symbol_target_and_reflog(tmp_path, packed, operation):
    repo = _repo(tmp_path / 'src')
    _git(repo, 'branch', '-M', 'main')
    oid = _oid(repo)
    ref = refs.custody_ref(STORE, OP, UNIT, oid)
    if packed:
        _git(repo, 'pack-refs', '--all')
    _git(repo, 'symbolic-ref', ref, 'refs/heads/main')
    before = (repo / '.git/logs/refs/heads/main').read_bytes()
    result = (refs.create_ref if operation == 'create' else refs.delete_exact)(repo, ref, oid)
    assert result['status'] not in {'created', 'deleted', 'reconciled'}
    assert _git(repo, 'symbolic-ref', ref).stdout.strip() == 'refs/heads/main'
    assert _git(repo, 'rev-parse', 'main').stdout.strip() == oid
    assert (repo / '.git/logs/refs/heads/main').read_bytes() == before


@pytest.mark.parametrize('kind', ['fifo', 'socket', 'directory', 'oversize', 'symlink', 'fifo-link', 'parent-link'])
@pytest.mark.parametrize('input_name', ['reflog', 'alternates'])
def test_reflog_planted_inputs_are_bounded_and_pending(tmp_path, kind, input_name):
    import socket
    repo = _repo(tmp_path / 'src')
    oid = _oid(repo)
    path = repo / ('.git/logs/HEAD' if input_name == 'reflog' else '.git/objects/info/alternates')
    raw = path.read_bytes() if input_name == 'reflog' else b' '
    if path.exists():
        path.unlink()
    sock = None
    def plant_socket():
        nonlocal sock
        sock = socket.socket(socket.AF_UNIX)
        previous = Path.cwd()
        try:
            os.chdir(path.parent)
            sock.bind(path.name)
        finally:
            os.chdir(previous)

    def plant_oversize():
        with path.open('wb') as stream:
            stream.write(raw * (16 * 1024 * 1024 // len(raw) + 1))

    def plant_link():
        target = tmp_path / 'target'
        if kind == 'fifo-link':
            os.mkfifo(target)
        else:
            target.write_bytes(raw)
        path.symlink_to(target)

    def plant_parent_link():
        path.write_bytes(raw)
        real = path.parent.with_name('kept-parent')
        path.parent.rename(real)
        path.parent.symlink_to(real, target_is_directory=True)

    {'fifo': lambda: os.mkfifo(path), 'directory': path.mkdir,
     'socket': plant_socket, 'oversize': plant_oversize, 'symlink': plant_link,
     'fifo-link': plant_link, 'parent-link': plant_parent_link}[kind]()
    expression = ('r.capture_reflog(Path(sys.argv[1]),lower_bound=0)' if input_name == 'reflog'
                  else 'r.verify_snapshot(Path(sys.argv[1]),sys.argv[2])')
    code = ('import sys; from pathlib import Path; from hermes_cli import kanban_source_refs as r; '
            f'x={expression}; '
            'assert x["status"] == "pending", x')
    child = subprocess.Popen([sys.executable, '-c', code, str(repo), oid], env=_env(),
                             cwd=Path(__file__).resolve().parents[2], start_new_session=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = child.communicate(timeout=5)
        assert child.returncode == 0, out + err
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate(timeout=5)
        if sock is not None:
            sock.close()


@pytest.mark.parametrize('kind', ['symbolic', 'direct', 'absent', 'outside'])
def test_restore_ref_is_create_only_without_fetch_side_effects(tmp_path, kind):
    source = _repo(tmp_path / 'source')
    dest = _repo(tmp_path / 'dest')
    _git(dest, 'branch', '-M', 'main')
    old = _oid(dest)
    (source / 'kept.txt').write_text('new source bytes\n')
    _git(source, 'commit', '-am', 'new source')
    oid = _oid(source)
    ref = refs.custody_ref(STORE, OP, UNIT, oid) if kind != 'outside' else 'refs/heads/foreign'
    _git(source, 'update-ref', ref, oid)
    if kind == 'symbolic':
        _git(dest, 'symbolic-ref', ref, 'refs/heads/main')
    elif kind == 'direct':
        _git(dest, 'update-ref', ref, old)
    log = dest / '.git/logs/refs/heads/main'
    before = log.read_bytes()
    fetch_head = dest / '.git/FETCH_HEAD'
    fetch_head.write_text('sentinel\n')
    _git(dest, 'config', 'remote.origin.url', str(source))
    _git(dest, 'config', 'remote.origin.fetch', '+refs/*:refs/mirror/*')
    result = refs.restore_ref(source, dest, ref)
    assert _git(dest, 'rev-parse', 'main').stdout.strip() == old
    assert log.read_bytes() == before
    assert fetch_head.read_text() == 'sentinel\n'
    assert not _git(dest, 'for-each-ref', 'refs/mirror').stdout
    if kind == 'absent':
        assert result['status'] == 'restored', result
        assert _git(dest, 'rev-parse', ref).stdout.strip() == oid
    else:
        assert result['status'] != 'restored', result
        if kind == 'symbolic':
            assert _git(dest, 'symbolic-ref', ref).stdout.strip() == 'refs/heads/main'
        elif kind == 'direct':
            assert _git(dest, 'rev-parse', ref).stdout.strip() == old
        else:
            assert _git(dest, 'show-ref', '--verify', ref, check=False).returncode != 0


def test_detached_repo_without_remote_retains_exact_bytes(tmp_path: Path):
    repo = _repo(tmp_path / "src")
    oid = _oid(repo)
    _git(repo, "checkout", "--detach")
    _git(repo, "remote")
    ref = refs.custody_ref(STORE, OP, UNIT, oid)
    created = refs.create_ref(repo, ref, oid)
    assert created["status"] == "created"
    verified = refs.verify_snapshot(repo, oid)
    assert verified["status"] == "complete"
    assert verified["missing"] == []
    blob = _git(repo, "cat-file", "-p", f"{oid}:kept.txt").stdout
    assert blob == "kept-bytes\n"


def test_missing_blob_is_incomplete_even_when_rev_list_exits_zero(tmp_path: Path):
    repo = _repo(tmp_path / "src")
    oid = _oid(repo)
    blob = _git(repo, "rev-parse", "HEAD:kept.txt").stdout.strip()
    gitdir = Path(_git(repo, "rev-parse", "--git-path", ".").stdout.strip())
    if not gitdir.is_absolute():
        gitdir = repo / gitdir
    obj = gitdir / "objects" / blob[:2] / blob[2:]
    obj.unlink()
    verified = refs.verify_snapshot(repo, oid)
    assert verified["status"] == "incomplete"
    assert blob in verified["missing"]


def test_alternates_stay_pending(tmp_path: Path):
    donor = _repo(tmp_path / "donor")
    repo = tmp_path / "borrower"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True, env=_env())
    alt = repo / ".git" / "objects" / "info" / "alternates"
    alt.parent.mkdir(parents=True, exist_ok=True)
    alt.write_text(str(donor / ".git" / "objects") + "\n", encoding="utf-8")
    oid = _oid(donor)
    verified = refs.verify_snapshot(repo, oid)
    assert verified["status"] == "pending"
    assert verified["reason"] == "alternates"


def test_reflog_old_and_new_oids_and_two_refs_for_one_oid(tmp_path: Path):
    repo = _repo(tmp_path / "src")
    first = _oid(repo)
    (repo / "kept.txt").write_text("next\n", encoding="utf-8")
    _git(repo, "commit", "-am", "next")
    second = _oid(repo)
    captured = refs.capture_reflog(repo, lower_bound=0)
    assert captured["status"] == "ok"
    assert first in captured["oids"] and second in captured["oids"]
    one = refs.create_ref(repo, refs.custody_ref(STORE, OP, "u1", second), second)
    two = refs.create_ref(repo, refs.custody_ref(STORE, OP, "u2", second), second)
    assert one["status"] == "created" and two["status"] == "created"
    assert refs.delete_exact(repo, one["ref"], second)["status"] == "deleted"
    assert _git(repo, "cat-file", "-e", second).returncode == 0


def test_create_only_and_wrong_oid_delete_do_not_drop_bytes(tmp_path: Path):
    repo = _repo(tmp_path / "src")
    oid = _oid(repo)
    ref = refs.custody_ref(STORE, OP, UNIT, oid)
    assert refs.create_ref(repo, ref, oid)["status"] == "created"
    (repo / "kept.txt").write_text("different-bytes\n", encoding="utf-8")
    _git(repo, "commit", "-am", "other object")
    other = _oid(repo)
    assert other != oid
    # Both objects exist: exercise CAS, not Git-version-specific ordering of
    # an existing-ref error versus a nonexistent-new-object error.
    again = refs.create_ref(repo, ref, other)
    assert again["status"] == "conflict"
    assert _git(repo, "rev-parse", ref).stdout.strip() == oid
    deleted = refs.delete_exact(repo, ref, other)
    assert deleted["status"] == "leaked"
    assert _git(repo, "rev-parse", ref).stdout.strip() == oid
    assert _git(repo, "cat-file", "-p", f"{ref}:kept.txt").stdout == "kept-bytes\n"


def test_nonexistent_object_cannot_create_or_replace_custody_ref(tmp_path: Path):
    repo = _repo(tmp_path / "src")
    oid = _oid(repo)
    missing = "a" * len(oid)
    assert _git(repo, "cat-file", "-e", missing, check=False).returncode != 0
    ref = refs.custody_ref(STORE, OP, UNIT, oid)
    absent = refs.create_ref(repo, ref, missing)
    assert absent["status"] == "invalid"
    assert _git(repo, "rev-parse", "--verify", ref, check=False).returncode != 0
    assert refs.create_ref(repo, ref, oid)["status"] == "created"
    refused = refs.create_ref(repo, ref, missing)
    # Git may check object existence before or after create-only preconditions.
    assert refused["status"] in {"invalid", "conflict"}
    assert _git(repo, "rev-parse", ref).stdout.strip() == oid
    assert _git(repo, "cat-file", "-p", f"{ref}:kept.txt").stdout == "kept-bytes\n"


def test_branch_delete_reflog_expiry_and_gc_keep_the_custody_ref(tmp_path: Path):
    repo = _repo(tmp_path / "src")
    oid = _oid(repo)
    ref = refs.custody_ref(STORE, OP, UNIT, oid)
    refs.create_ref(repo, ref, oid)
    _git(repo, "checkout", "--orphan", "empty")
    _git(repo, "branch", "-D", "master", check=False)
    _git(repo, "branch", "-D", "main", check=False)
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "--prune=now")
    assert _git(repo, "cat-file", "-p", f"{oid}:kept.txt").stdout == "kept-bytes\n"
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    subprocess.run(["git", "init", str(fresh)], check=True, capture_output=True, text=True, env=_env())
    fetched = refs.restore_ref(repo, fresh, ref)
    assert fetched["status"] == "restored"
    assert _git(fresh, "cat-file", "-p", f"{oid}:kept.txt").stdout == "kept-bytes\n"


def test_unparseable_reflog_stays_pending(tmp_path: Path):
    repo = _repo(tmp_path / "src")
    log = repo / ".git" / "logs" / "HEAD"
    log.write_text(log.read_text(encoding="utf-8") + "not a reflog record\n", encoding="utf-8")
    captured = refs.capture_reflog(repo, lower_bound=0)
    assert captured["status"] == "pending"
    assert captured["oids"]


def test_timeout_does_not_delete_a_git_lock(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path / "src")
    oid = _oid(repo)
    lock = repo / ".git" / "packed-refs.lock"
    lock.write_text("held\n", encoding="utf-8")

    def explode(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=0.01)

    monkeypatch.setattr(refs.subprocess, "run", explode)
    result = refs.create_ref(repo, refs.custody_ref(STORE, OP, UNIT, oid), oid, budget=lambda cap: cap)
    assert result["status"] == "unknown"
    assert lock.read_text(encoding="utf-8") == "held\n"
    assert not os.path.exists(str(lock) + ".deleted")


def test_sha256_repository_round_trip_when_git_supports_it(tmp_path: Path):
    probe = subprocess.run(["git", "init", "--object-format=sha256", str(tmp_path / "probe")],
                           capture_output=True, text=True, env=_env(), check=False)
    if probe.returncode != 0:
        pytest.fail(f"git cannot create a sha256 repository: {probe.stderr}")
    repo = _repo(tmp_path / "sha", fmt="sha256")
    oid = _oid(repo)
    assert len(oid) == 64
    ref = refs.custody_ref(STORE, OP, UNIT, oid)
    assert refs.create_ref(repo, ref, oid)["status"] == "created"
    assert refs.verify_snapshot(repo, oid)["status"] == "complete"


def test_refs_do_not_signal(monkeypatch, tmp_path: Path):
    called = []
    monkeypatch.setattr(os, "kill", lambda *a: called.append(a))
    repo = _repo(tmp_path / "src")
    refs.verify_snapshot(repo, _oid(repo))
    assert called == []


def test_empty_message_reflog_and_non_utf8_stay_distinct(tmp_path: Path):
    repo = _repo(tmp_path / "src")
    _git(repo, "commit", "--allow-empty", "-m", "second")
    _git(repo, "update-ref", "HEAD", "HEAD~1")
    parsed = refs.capture_reflog(repo, lower_bound=0)
    assert parsed["status"] == "ok"
    assert parsed["oids"]
    log = repo / ".git" / "logs" / "HEAD"
    log.write_bytes(log.read_bytes() + b"\xff not utf8\n")
    pending = refs.capture_reflog(repo, lower_bound=0)
    assert pending["status"] == "pending"
    assert pending["reason"] == "unparseable"


def test_submodule_gitlink_is_unresolved_inventory(tmp_path: Path):
    repo = _repo(tmp_path / "src")
    oid = _oid(repo)
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{oid},vendor/lib")
    _git(repo, "commit", "-m", "link")
    verified = refs.verify_snapshot(repo, _oid(repo))
    assert verified["status"] == "incomplete"
    assert verified["reason"] == "submodule"
    assert verified["submodules"][0]["path"] == "vendor/lib"
    assert verified["submodules"][0]["oid"] == oid


def test_format_mismatch_is_invalid_and_identity_is_retained(tmp_path: Path):
    repo = _repo(tmp_path / "src")
    oid = _oid(repo)
    mismatch = refs.create_ref(repo, refs.custody_ref(STORE, OP, UNIT, "a" * 64), "a" * 64)
    assert mismatch["status"] == "invalid"
    assert mismatch["reason"] == "object_format"
    created = refs.create_ref(repo, refs.custody_ref(STORE, OP, UNIT, oid), oid)
    assert created["status"] == "created"
    assert created["identity"]["status"] == "ok"
    assert isinstance(created["identity"]["ino"], int)


def test_env_alternates_stay_pending(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path / "src")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(tmp_path / "borrowed"))
    verified = refs.verify_snapshot(repo, _oid(repo))
    assert verified["status"] == "pending"
    assert verified["reason"] == "alternates"
