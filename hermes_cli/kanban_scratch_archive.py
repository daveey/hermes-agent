"""Archive-then-remove for aged terminal scratch workspaces holding git clones. [daveey]

Completion cleanup and ``gc_task_scratch`` keep every scratch tree that holds
Git metadata, so a worker's clone survives its card forever. Past the
retention window (``kanban.worktree_retention_hours`` when set, else
``kanban.worktree_retention_days``; see ``kanban_worktree_gc.retention_days``)
this module saves each standalone repo's unique work under
``<kanban home>/archive/scratch/<board>/<task>/<relpath>/``:

* ``commits.bundle``: refs and HEAD not reachable from the repo's
  remote-tracking refs (omitted when there are none), verified before use;
* ``tracked.diff``: ``git diff --binary HEAD`` (omitted when empty);
* ``untracked.tar``: ``git ls-files --others --exclude-standard`` (omitted when
  empty);
* ``remote_base.json`` / ``remote_base.tar`` (partial clones only): paths whose
  HEAD blob only the promisor remote holds, left out of ``tracked.diff`` and
  never fetched, plus the worktree side of those still present;
* ``meta.json``: remotes, HEAD, branch, task id, completed_at.

Every git call runs offline (no lazy fetch, no transport): a blobless clone
with no credential used to be refused here forever.

Alongside those (layout v1 unchanged) every archive also carries a complete,
versioned filesystem payload under ``.hermes-complete-archive-v1/``:
``tree.tar.gz`` (every file, empty directory, mode and symlink of the
workspace, ignored and sibling files and ``.git`` included, symlinks stored as
links and never dereferenced) and ``manifest.json`` (per-entry type, mode,
size, link target, sha256, plus the payload hash). The payload is verified
against the manifest and the source tree is re-scanned for mutation before the
archive is published. Trees with no git repository are archived by the payload
alone. That directory name is reserved: a workspace entry with that name is
refused so the metadata can never collide with archived content.
``tree.tar.gz`` is written at gzip level 1 (``_GZIP_LEVEL``), which trades
about 14% more archive bytes for ~7x faster reclaim (see the constant for the
measurements and how to reverse it).

When a repo's remote-tracking refs (of a remote with a network URL) cover its
history, the payload leaves out what that remote already holds: the files under
``.git/objects/`` (replaced by one pack of the local-only objects) and clean
tracked files (recorded by blob id and hash). The manifest is then version 2,
the repo's meta.json says ``objects_omitted: "remote"`` with the restore steps,
and ``kanban_scratch_archive_omit.restore`` rebuilds the tree from the remote.
A full core clone archived to 0.7 % of its bytes this way (was 57-80 %).

Anything it cannot prove safe keeps the workspace: a linked worktree or
submodule (a ``.git`` gitlink file or symlink: the worktree GC owns those; a
regular ``.git`` file with no ``gitdir:`` line, such as uv's empty cache
marker, is plain data), a bare repository,
a special file (fifo/socket/device), any scan error, source mutation during
the archive, or any failed archive step. Symlinks are recorded, never
followed.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

_GIT_TIMEOUT = 600


class ArchiveRefused(Exception):
    """The tree must be kept; the message says why."""


def find_repos(root: Path) -> list[Path]:
    """Standalone repos (a ``.git`` directory) below *root*, *root* included.

    Raises :class:`ArchiveRefused` for a linked worktree/submodule, a bare
    repository, a non-directory root, or any scan error. Traverses by directory
    descriptor (never follows symlinks, ancestors included). A regular ``.git``
    file that is not a ``gitdir:`` pointer (uv writes an empty one in each cache
    dir) is plain data, archived in the payload like any other file.
    """
    return _find_repos(root)[0]


_GITLINK_PREFIX = b"gitdir:"
_GITLINK_PROBE = 4096


def _is_gitlink(dfd: int, directory: Path, st: os.stat_result) -> bool:
    """True unless the regular file ``.git`` under *dfd* (lstat *st*) is provably not a gitlink.

    Handles regular files only: the caller treats any other ``.git`` type
    (symlink, fifo, ...) as a gitlink without calling this. *dfd* is the scan's
    own descriptor for *directory* (used only in messages), so the read is tied
    to the directory the scan saw rather than a fresh walk of the path. Opened
    without following links and checked against the scanned lstat, so the bytes
    read belong to the entry the scan recorded. Git only accepts a file starting
    ``gitdir: ``; leading whitespace is tolerated here too, so the rule errs
    towards keeping the tree.
    """
    fd = os.open(".git", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    with os.fdopen(fd, "rb") as fh:
        fst = os.fstat(fh.fileno())
        if not stat.S_ISREG(fst.st_mode) or _signature(fst) != _signature(st):
            raise ArchiveRefused(f"{directory / '.git'} changed during scan")
        head = fh.read(_GITLINK_PROBE)
    return head.lstrip().startswith(_GITLINK_PREFIX)


def _in_git_metadata(rel: str) -> bool:
    return rel == ".git" or rel.endswith("/.git") or rel.startswith(".git/") or "/.git/" in rel


def _find_repos(root: Path) -> tuple[list[Path], dict[str, list[int]]]:
    """``find_repos`` plus the signatures of the ``.git`` data files it judged.

    The caller re-checks those signatures against the payload snapshot, so a
    marker rewritten into a gitlink after this judgement is refused. Each
    regular ``.git`` file is judged during the scan, through the scan's own
    descriptor for its directory.
    """
    verdicts: dict[str, bool] = {}

    def judge(dfd: int, gitrel: str, st: os.stat_result) -> None:
        rel = gitrel.rpartition("/")[0] or "."
        if not _in_git_metadata(rel):
            verdicts[gitrel] = _is_gitlink(dfd, root if rel == "." else root / rel, st)

    found = _scan(root, on_git_file=judge)
    markers: dict[str, list[int]] = {}
    kids: dict[str, set[str]] = {".": set()}
    for rel, st in found.items():
        if rel == ".":
            continue
        parent, _, name = rel.rpartition("/")
        kids.setdefault(parent or ".", set()).add(name)
        if stat.S_ISDIR(st.st_mode):
            kids.setdefault(rel, set())
    repos: list[Path] = []
    for rel, names in kids.items():
        if _in_git_metadata(rel):
            continue  # never descend into repository metadata
        directory = root if rel == "." else root / rel
        if ".git" in names:
            gitrel = ".git" if rel == "." else f"{rel}/.git"
            gst = found[gitrel]
            if stat.S_ISDIR(gst.st_mode):
                repos.append(directory)
                continue
            # A marker the scan never judged counts as a gitlink (fail closed).
            if not stat.S_ISREG(gst.st_mode) or verdicts.get(gitrel, True):
                raise ArchiveRefused(f"linked worktree or submodule at {directory / '.git'} (left to the worktree GC)")
            markers[gitrel] = _signature(gst)  # plain data (e.g. uv's empty cache marker)
        if {"HEAD", "objects"} <= names and names & {"refs", "reftable"}:
            raise ArchiveRefused(f"bare repository at {directory}")
    return sorted(repos), markers


def _env(repo: Path) -> dict[str, str]:
    """Hardened, offline git env: archiving reads local objects only and never fetches.

    A partial clone's missing objects would otherwise be lazily fetched from its
    promisor remote (unbounded network, or a credential failure on workers). Git
    >= 2.44 honours ``GIT_NO_LAZY_FETCH``; ``protocol.allow=never`` also fails
    every transport on older git (2.43 on the pool workers) before it connects.
    """
    from hermes_cli._subprocess_compat import NO_LAZY_FETCH_ENV, noninteractive_repo_git_env
    env = noninteractive_repo_git_env(repo)
    if env is None:
        raise ArchiveRefused(f"cannot harden git for {repo} (filter discovery failed)")
    count = int(env.get("GIT_CONFIG_COUNT") or 0)
    env.update(NO_LAZY_FETCH_ENV)
    env.update({f"GIT_CONFIG_KEY_{count}": "protocol.allow", f"GIT_CONFIG_VALUE_{count}": "never",
                "GIT_CONFIG_COUNT": str(count + 1)})
    return env


def _git(repo: Path, env: dict[str, str], *args: str, ok=(0,)) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, timeout=_GIT_TIMEOUT,
        check=False, stdin=subprocess.DEVNULL, env=env,
    )
    if proc.returncode not in ok:
        err = proc.stderr.decode("utf-8", "replace").strip()[:500]
        raise ArchiveRefused(f"git {' '.join(args[:3])} failed in {repo} (rc {proc.returncode}): {err}")
    return proc


def _text(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.decode("utf-8", "replace").strip()


def _is_partial_clone(repo: Path, env: dict[str, str]) -> bool:
    """Whether *repo* has a promisor remote (objects it may lack are promised, not lost).

    Read with git's own semantics: ``remote.<name>.promisor`` is a bool (an empty
    ``promisor =`` is false; a value git cannot parse refuses), and
    ``extensions.partialClone`` names the promisor remote, so only a non-empty
    name counts. Anything else is a full clone, whose missing objects refuse.
    """
    proc = _git(repo, env, "config", "--null", "--type=bool", "--get-regexp", r"^remote\..*\.promisor$",
                ok=(0, 1))
    if any(entry.rpartition("\n")[2] == "true" for entry in _text(proc).split("\0") if entry):
        return True
    return bool(_text(_git(repo, env, "config", "--get", "extensions.partialclone", ok=(0, 1))))


def _raw_changes(repo: Path, env: dict[str, str], base: str) -> list[dict]:
    """``git diff --raw`` of *base* against the worktree: reads no blob content, so it never fetches."""
    out = _git(repo, env, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", "--no-ext-diff", base).stdout
    fields = out.split(b"\0")
    changes = []
    for info, path in zip(fields[0::2], fields[1::2]):
        # ":<old mode> <new mode> <old oid> <new oid> <status>"; anything else raises ValueError (kept).
        old_mode, _new_mode, old_oid, _new_oid, status = info[1:].decode("ascii").split()
        changes.append({"status": status, "old_mode": old_mode, "old_oid": old_oid,
                        "path": os.fsdecode(path)})
    return changes


def _missing_objects(repo: Path, env: dict[str, str]) -> set[str]:
    """Objects of HEAD's tree absent locally (left on the promisor remote), listed without fetching."""
    out = _text(_git(repo, env, "rev-list", "--objects", "--missing=print", "--no-walk", "HEAD"))
    return {line[1:].split()[0] for line in out.splitlines() if line.startswith("?")}


def _batches(paths: list[str], limit: int = 64 * 1024) -> list[list[str]]:
    """Split *paths* into argv-sized batches (well under ARG_MAX)."""
    out: list[list[str]] = []
    size = limit
    for path in paths:
        if size + len(path) + 1 > limit:
            out.append([])
            size = 0
        out[-1].append(path)
        size += len(path) + 1
    return out


def _write_remote_base(repo: Path, dest: Path, entries: list[dict]) -> list[str]:
    """Record paths whose HEAD blob only the promisor remote holds; keep their worktree side.

    Their base is not local work (git promises it is on the remote), so it is not
    fetched. Whatever the worktree holds at such a path is local and goes into
    ``remote_base.tar`` (stored as is, symlinks never followed).
    """
    (dest / "remote_base.json").write_text(json.dumps({
        "note": "HEAD-side blobs of these paths are only on the promisor remote; not fetched",
        "entries": entries,
    }, indent=1) + "\n", encoding="utf-8")
    present = [e["path"] for e in entries if os.path.lexists(repo / e["path"])]
    if not present:
        return ["remote_base.json"]
    with tarfile.open(dest / "remote_base.tar", "w") as tar:
        for rel in present:
            tar.add(str(repo / rel), arcname=rel, recursive=False)
    return ["remote_base.json", "remote_base.tar"]


def archive_repo(repo: Path, dest: Path, *, task_id: str, completed_at: Optional[int],
                 omitted: Optional[dict] = None) -> dict:
    """Write one repo's archive into *dest* (created). Raises on any failure.

    *omitted* is this repo's ``kanban_scratch_archive_omit`` record when the
    payload leaves out objects the remote holds; meta.json then says so and
    lists the restore steps.
    """
    env = _env(repo)
    dest.mkdir(parents=True, exist_ok=True)  # the root repo archives into the fresh partial dir
    head = _text(_git(repo, env, "rev-parse", "--verify", "-q", "HEAD^{commit}", ok=(0, 1))) or None
    branch = _text(_git(repo, env, "symbolic-ref", "-q", "--short", "HEAD", ok=(0, 1))) or None
    remotes: dict[str, str] = {}
    for line in _text(_git(repo, env, "remote", "-v")).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            remotes.setdefault(parts[0], parts[1])

    # Commits not reachable from any remote-tracking ref.
    refs = [r for r in _text(_git(repo, env, "for-each-ref", "--format=%(refname)")).splitlines()
            if r and not r.startswith("refs/remotes/")]
    if head:
        refs.append("HEAD")
    unpushed = [r for r in refs
                if _text(_git(repo, env, "rev-list", "-n1", r, "--not", "--remotes"))]
    files = ["meta.json"]
    if unpushed:
        bundle = dest / "commits.bundle"
        _git(repo, env, "bundle", "create", str(bundle), *unpushed, "--not", "--remotes")
        _git(repo, env, "bundle", "verify", str(bundle))
        files.append(bundle.name)

    base = head or _text(_git(repo, env, "hash-object", "-t", "tree", "--stdin"))
    changes = _raw_changes(repo, env, base) if head and _is_partial_clone(repo, env) else []
    missing = _missing_objects(repo, env) if changes else set()
    remote_base = [c for c in changes if c["old_oid"] in missing]
    diff_args = ["diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-color", base]
    if not remote_base:
        diff = _git(repo, env, *diff_args).stdout
    else:
        # Only paths whose base blob is local, literal pathspecs in bounded batches (no renames
        # across batches); the rest are recorded below, never fetched.
        keep = [c["path"] for c in changes if c["old_oid"] not in missing]
        literal = dict(env, GIT_LITERAL_PATHSPECS="1")
        diff = b"".join(_git(repo, literal, *diff_args, "--no-renames", "--", *batch).stdout
                        for batch in _batches(keep))
    if diff:
        (dest / "tracked.diff").write_bytes(diff)
        files.append("tracked.diff")
    extra: dict = {}
    if omitted is not None:
        from hermes_cli.kanban_scratch_archive_omit import RESTORE_STEPS
        extra.update(objects_omitted=omitted["reason"], objects_restore=RESTORE_STEPS,
                     omitted_remote_refs=omitted["remote_refs"], supplement_pack=omitted["pack"])
    if remote_base:
        files.extend(_write_remote_base(repo, dest, remote_base))
        extra["remote_base_count"] = len(remote_base)

    raw = _git(repo, env, "ls-files", "--others", "--exclude-standard", "-z").stdout
    untracked = [os.fsdecode(p) for p in raw.split(b"\0") if p]
    if untracked:
        with tarfile.open(dest / "untracked.tar", "w") as tar:
            for rel in untracked:
                # Embedded repos are listed as directories; they are archived on their own.
                tar.add(str(repo / rel.rstrip("/")), arcname=rel.rstrip("/"), recursive=False)
        files.append("untracked.tar")

    meta = {
        "task_id": task_id, "completed_at": completed_at, "source": str(repo),
        "head": head, "branch": branch, "remotes": remotes, "unpushed_refs": unpushed,
        "untracked_count": len(untracked), "files": files, "archived_at": int(time.time()), **extra,
    }
    (dest / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


PAYLOAD_DIR = ".hermes-complete-archive-v1"
PAYLOAD_VERSION = 1
# Version 2: objects the remote holds are left out (kanban_scratch_archive_omit). An older
# verify_complete_archive refuses it rather than calling a payload with omissions complete.
PAYLOAD_VERSION_OMITTED = 2
_TREE = "tree.tar.gz"
_MANIFEST = "manifest.json"
# Level 1 trades size for reclaim speed: archives are ~14% larger than at level 9
# (547 MB worker tree: L9 59.8 s / 180.2 MB, L6 18.6 s / 181.6 MB, L1 8.3 s / 205.2 MB),
# so budget archive/scratch disk for that. To reverse, set 6 (near-L9 size at ~2x L1
# time); readers need no change, every level reads back through "r:gz".
_GZIP_LEVEL = 1


def _kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "symlink"
    raise ArchiveRefused("special file in workspace (fifo/socket/device)")


def _signature(st: os.stat_result) -> list[int]:
    return [st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_dev,
            st.st_uid, st.st_gid, st.st_nlink]


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_root(root: Path) -> int:
    """Anchor at / (or cwd), opening every path component without following links."""
    root = Path(root)
    fd = os.open(root.anchor or ".", _DIR_FLAGS)
    try:
        for part in root.parts[1:] if root.is_absolute() else root.parts:
            if part == "..":
                raise ArchiveRefused("workspace path contains parent traversal")
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


class _HashingReader:
    def __init__(self, fh):
        self.fh, self.digest, self.count = fh, hashlib.sha256(), 0

    def read(self, n=-1):
        data = self.fh.read(n)
        self.digest.update(data)
        self.count += len(data)
        return data


def _scan(root: Path, tar: Optional[tarfile.TarFile] = None,
          entries: Optional[dict] = None, *,
          on_git_file: Optional[Callable[[int, str, os.stat_result], None]] = None,
          omit: Optional[Callable[[int, str, str, os.stat_result], bool]] = None,
          ) -> dict[str, os.stat_result]:
    """lstat of every entry below *root* (``"."`` is the root), by directory descriptor.

    Every directory is opened ``O_DIRECTORY|O_NOFOLLOW`` relative to its parent
    descriptor and every stat/open/readlink is relative to a descriptor, so a
    symlink swapped in for any ancestor is refused rather than followed. With
    *tar*/*entries* the content is archived during the same traversal.
    *on_git_file(dfd, rel, lstat)* is called for each regular ``.git`` file
    while its directory's descriptor *dfd* is still open. *omit(dfd, name, rel,
    lstat)* returning True leaves that regular file out of *tar* (it records the
    file itself; see ``kanban_scratch_archive_omit.Omitter``).
    """
    found: dict[str, os.stat_result] = {}
    seen_inodes: dict[tuple[int, int], str] = {}

    def visit(dfd: int, rel: str) -> None:
        for name in sorted(os.listdir(dfd)):
            erel = name if rel == "." else f"{rel}/{name}"
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            kind = _kind(st.st_mode)
            found[erel] = st
            if on_git_file is not None and name == ".git" and stat.S_ISREG(st.st_mode):
                on_git_file(dfd, erel, st)
            info = rec = None
            if tar is not None:
                info = tarfile.TarInfo(erel)
                info.mode, info.uid, info.gid = stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid
                info.mtime = int(st.st_mtime)
                rec = {"type": kind, "mode": info.mode}
            if kind == "dir":
                if info is not None:
                    info.type = tarfile.DIRTYPE
                    tar.addfile(info)
                    entries[erel] = rec
                cfd = os.open(name, _DIR_FLAGS, dir_fd=dfd)
                try:
                    cst = os.fstat(cfd)
                    if (cst.st_ino, cst.st_dev) != (st.st_ino, st.st_dev):
                        raise ArchiveRefused(f"{erel} changed during scan")
                    visit(cfd, erel)
                finally:
                    os.close(cfd)
            elif kind == "symlink":
                if info is not None:
                    info.type, info.linkname = tarfile.SYMTYPE, os.readlink(name, dir_fd=dfd)
                    tar.addfile(info)
                    rec["link"] = info.linkname
                    entries[erel] = rec
            elif info is not None:
                if omit is not None and omit(dfd, name, erel, st):
                    continue
                key = (st.st_dev, st.st_ino)
                if st.st_nlink > 1 and key in seen_inodes:
                    info.type, info.linkname = tarfile.LNKTYPE, seen_inodes[key]
                    tar.addfile(info)
                    rec.update(type="hardlink", link=info.linkname)
                    entries[erel] = rec
                    continue
                # A regular file may have become a FIFO since lstat. Never block
                # opening it, and reject its actual descriptor type before reading.
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
                with os.fdopen(fd, "rb") as fh:
                    fst = os.fstat(fh.fileno())
                    if not stat.S_ISREG(fst.st_mode) or _signature(fst) != _signature(st):
                        raise ArchiveRefused(f"{erel} changed during archive")
                    info.type, info.size = tarfile.REGTYPE, fst.st_size
                    reader = _HashingReader(fh)
                    tar.addfile(info, reader)
                if reader.count != info.size:
                    raise ArchiveRefused(f"{erel} changed size during archive")
                rec.update(size=info.size, sha256=reader.digest.hexdigest())
                entries[erel] = rec
                if st.st_nlink > 1:
                    seen_inodes[key] = erel

    try:
        rfd = _open_root(root)
    except OSError as exc:
        raise ArchiveRefused(f"{root} is not a plain directory: {exc!r}") from exc
    try:
        rst = os.fstat(rfd)
        found["."] = rst
        if tar is not None:
            info = tarfile.TarInfo(".")
            info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, stat.S_IMODE(rst.st_mode), rst.st_uid, rst.st_gid
            info.mtime = int(rst.st_mtime)
            tar.addfile(info)
            entries["."] = {"type": "dir", "mode": info.mode}
        visit(rfd, ".")
    except OSError as exc:
        raise ArchiveRefused(f"scan uncertain: {exc!r}") from exc
    finally:
        os.close(rfd)
    return found


def _signatures(found: dict[str, os.stat_result]) -> dict[str, list[int]]:
    return {k: _signature(v) for k, v in found.items()}


def check_snapshot(workspace: Path, snapshot: dict[str, list[int]], *, renamed: bool = False) -> None:
    """Raise :class:`ArchiveRefused` unless *workspace* still matches the archived *snapshot*.

    ``renamed`` is for a tree just moved aside: the root's own ctime/nlink may
    change with the rename, every descendant (and the root's mtime) still must not.

    This is mutation detection, not a filesystem snapshot or writer exclusion:
    an open descriptor can modify an already visited entry even after rename.
    Callers must retain live-job/session/custody exclusion independently.
    """
    now = _signatures(_scan(workspace))
    want = {k: list(v) for k, v in snapshot.items()}
    if renamed:
        for sig in (now["."], want["."]):
            sig[3] = sig[8] = 0
    if now != want:
        raise ArchiveRefused("workspace changed after it was archived")


def _write_payload(workspace: Path, dest: Path, omitter=None,
                   omitted_repos: Optional[list[dict]] = None) -> dict[str, list[int]]:
    """Write ``tree.tar.gz`` + ``manifest.json`` into *dest*; return the source signatures.

    With *omitter* (``kanban_scratch_archive_omit.Omitter``) the files it drops
    are listed under ``omitted`` and *omitted_repos* records how to get them
    back; that manifest is version 2.
    """
    entries: dict[str, dict] = {}
    tree = dest / _TREE
    with open(tree, "wb") as raw:
        # mtime=0 keeps the gzip header deterministic. Level 1, not the default 9:
        # archive time bounds reclaim throughput (size/time trade-off at _GZIP_LEVEL).
        # Any level reads back through the same "r:gz" path.
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=_GZIP_LEVEL) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                before = _scan(workspace, tar, entries, omit=omitter)
    manifest = {
        "version": PAYLOAD_VERSION, "tree": _TREE, "tree_sha256": _sha256_file(tree),
        "entries": entries,
    }
    if omitted_repos and omitter is not None:
        manifest.update(version=PAYLOAD_VERSION_OMITTED, omitted=omitter.omitted, omitted_repos=omitted_repos)
    (dest / _MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    snapshot = _signatures(before)
    if _signatures(_scan(workspace)) != snapshot:
        raise ArchiveRefused("workspace changed while it was being archived")
    return snapshot


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_complete_archive(archive: Path) -> dict:
    """Re-read the payload and prove it matches its manifest; raise :class:`ArchiveRefused` if not."""
    base = Path(archive) / PAYLOAD_DIR
    try:
        manifest = json.loads((base / _MANIFEST).read_text(encoding="utf-8-sig"))
        if manifest.get("version") not in (PAYLOAD_VERSION, PAYLOAD_VERSION_OMITTED):
            raise ArchiveRefused(f"unsupported payload version {manifest.get('version')!r}")
        if manifest["version"] == PAYLOAD_VERSION_OMITTED:
            from hermes_cli.kanban_scratch_archive_omit import verify_omissions
            verify_omissions(base, manifest)
        if _sha256_file(base / _TREE) != manifest["tree_sha256"]:
            raise ArchiveRefused("payload hash mismatch")
        want, seen = manifest["entries"], set()
        with tarfile.open(base / _TREE, "r:gz") as tar:
            for m in tar:
                rec = want.get(m.name)
                if rec is None or m.name in seen:
                    raise ArchiveRefused(f"unexpected payload member {m.name!r}")
                seen.add(m.name)
                kind = "hardlink" if m.islnk() else "file" if m.isreg() else "dir" if m.isdir() \
                    else "symlink" if m.issym() else "other"
                if kind != rec["type"] or stat.S_IMODE(m.mode) != rec["mode"]:
                    raise ArchiveRefused(f"payload type/mode mismatch for {m.name!r}")
                if kind in ("symlink", "hardlink") and m.linkname != rec["link"]:
                    raise ArchiveRefused(f"payload link mismatch for {m.name!r}")
                if kind == "file":
                    digest = hashlib.sha256()
                    fh = tar.extractfile(m)
                    for block in iter(lambda: fh.read(1 << 20), b""):
                        digest.update(block)
                    if digest.hexdigest() != rec["sha256"] or m.size != rec["size"]:
                        raise ArchiveRefused(f"payload content mismatch for {m.name!r}")
        if seen != set(want):
            raise ArchiveRefused("payload is missing members")
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, EOFError) as exc:
        raise ArchiveRefused(f"payload unreadable: {exc!r}") from exc
    return manifest


def _fsync_path(path: Path, *, directory: bool) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | (getattr(os, "O_DIRECTORY", 0) if directory else 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(root: Path) -> None:
    """Flush every file, then every directory, bottom-up; any failure propagates (fail closed)."""
    for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            full = Path(current) / name
            if not full.is_symlink():
                _fsync_path(full, directory=False)
        _fsync_path(Path(current), directory=True)


def _rename_noreplace(source: Path, target: Path) -> None:
    """Atomic rename which cannot replace a recreated original, even an empty dir."""
    import ctypes
    import errno
    import sys

    libc = ctypes.CDLL(None, use_errno=True)
    symbol = "renameat2" if sys.platform == "linux" else "renamex_np" if sys.platform == "darwin" else ""
    rename = getattr(libc, symbol, None) if symbol else None
    if rename is None:
        raise OSError(errno.ENOTSUP, "atomic no-replace directory rename unavailable")
    if sys.platform == "linux":
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)  # RENAME_NOREPLACE
    else:
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(source), os.fsencode(target), 4)  # RENAME_EXCL
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def remove_archived(conn, path: Path, archive: ArchivedPath, guard, finalize) -> int:
    """Scan outside short Board detach/finalize fences, retaining a recovery intent.

    guard(tree) checks current ownership/custody and known live jobs using the
    original path, but checks existence at tree. The durable intent names both
    locations and the verified archive before rename. Interrupted operations
    leave it for manual recovery; it NEVER authorizes automatic retry deletion.
    No recursive scan or rmtree holds the Board writer lock. Known-job exclusion
    remains independent: an unregistered writer with an open FD can write after
    the last scan. This is mutation detection, not a perfect filesystem snapshot.
    """
    import uuid
    from hermes_cli import kanban_db as kb

    if not guard(path):
        return 0
    check_snapshot(path, archive.snapshot)
    token = uuid.uuid4().hex
    trash = path.with_name(f".{path.name}.gc-{token}")
    intent = archive / f"detach-{token}.json"
    record = {"version": 1, "original": str(path), "detached": str(trash),
              "archive": str(archive), "root_identity": archive.snapshot["."]}
    with intent.open("x", encoding="utf-8") as fh:
        json.dump(record, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    # Flush the receipt's directory and the archive's published name before detach.
    for directory in (archive, archive.parent):
        afd = _open_root(directory)
        try:
            os.fsync(afd)
        finally:
            os.close(afd)

    def identity(tree, renamed=False):
        now, want = _signature(tree.lstat()), list(archive.snapshot["."])
        if renamed:
            for sig in (now, want):
                sig[3] = sig[8] = 0
        return now == want and stat.S_ISDIR(now[0])

    detached = False
    try:
        with kb.write_txn(conn):
            if not guard(path) or not identity(path):
                return 0
            _rename_noreplace(path, trash)
            detached = True
        check_snapshot(trash, archive.snapshot, renamed=True)
        with kb.write_txn(conn):
            if os.path.lexists(path) or not guard(trash) or not identity(trash, renamed=True):
                raise ArchiveRefused("ownership, live-job guard or tree identity changed after detach")
            finalize()
    except BaseException:
        if detached:
            try:
                _rename_noreplace(trash, path)
            except OSError as exc:
                log.error("Scratch detached tree preserved at %s; recovery intent=%s; restore failed: %s",
                          trash, intent, exc)
        raise
    # A later pickup gets a new tree. Keep intent on partial rmtree failures.
    shutil.rmtree(trash)
    intent.unlink()
    return 1


class ArchivedPath(type(Path())):
    """The final archive directory, carrying the source ``snapshot`` it was built from."""

    snapshot: dict


def _omission_plans(workspace: Path, repos: list[Path], payload: Path) -> tuple[list[dict], dict]:
    """Per repo, what the payload may leave out because the remote holds it (see kanban_scratch_archive_omit).

    Returns (plans, {repo: manifest record}) in *repos* order; a plan's index is
    its record's index in the manifest's ``omitted_repos``. Supplement packs are
    written into *payload* here, before the payload scan.
    """
    from hermes_cli import kanban_scratch_archive_omit as om
    plans, records = [], {}
    for repo in repos:
        env = _env(repo)
        plan = om.plan_repo(repo, workspace, env, _git, partial=_is_partial_clone(repo, env))
        if plan is None:
            continue
        records[repo] = om.write_supplement(repo, env, plan, payload, len(plans))
        plans.append(plan)
    return plans, records


def archive_workspace(workspace: Path, archive_dir: Path, *, task_id: str,
                      completed_at: Optional[int]) -> ArchivedPath:
    """Archive every standalone repo in *workspace* to *archive_dir*; return the final dir (an :class:`ArchivedPath`
    whose ``snapshot`` the caller must re-check at detach with :func:`check_snapshot`).

    Built in a ``.partial`` sibling and renamed into place, so a failure leaves
    no half archive that looks complete. Raises :class:`ArchiveRefused` (or
    ``OSError``) on any failure; the caller then keeps the workspace.
    """
    repos, markers = _find_repos(workspace)
    try:
        reserved = os.path.lexists(workspace / PAYLOAD_DIR)
    except OSError as exc:
        raise ArchiveRefused(f"scan uncertain: {exc!r}") from exc
    if reserved:
        raise ArchiveRefused(f"workspace entry {PAYLOAD_DIR} collides with archive metadata")
    partial = archive_dir.with_name(archive_dir.name + f".partial-{os.getpid()}")
    shutil.rmtree(partial, ignore_errors=True)
    try:
        partial.mkdir(parents=True)
        payload = partial / PAYLOAD_DIR
        payload.mkdir()
        plans, records = _omission_plans(workspace, repos, payload)
        for repo in repos:
            rel = repo.relative_to(workspace)
            archive_repo(repo, partial / rel if rel.parts else partial,
                         task_id=task_id, completed_at=completed_at, omitted=records.get(repo))
        omitter = None
        if plans:
            from hermes_cli.kanban_scratch_archive_omit import Omitter
            omitter = Omitter(plans)
        # Last, so nothing (including git's own index refresh) mutates the tree after the scan.
        snapshot = (_write_payload(workspace, payload, omitter, [records[r] for r in repos if r in records])
                    if plans else _write_payload(workspace, payload))
        if any(snapshot.get(rel) != sig for rel, sig in markers.items()):
            raise ArchiveRefused("a .git data file changed after it was judged not a gitlink")
        if plans:
            from hermes_cli.kanban_scratch_archive_omit import check_files_unchanged
            check_files_unchanged(plans, snapshot)
        verify_complete_archive(partial)
        _fsync_tree(partial)  # payload, manifest, repo bundles and all directories, before publication
        final = archive_dir
        if final.exists():
            final = archive_dir.with_name(f"{archive_dir.name}-{int(time.time())}")
        partial.rename(final)
        try:
            # Persist every ancestor entry, not just the published name. Existing
            # directories may come from an interrupted prior attempt whose mkdir
            # was never flushed, so existence cannot serve as a durability fence.
            for parent in final.absolute().parents:
                _fsync_path(parent, directory=True)
        except BaseException:
            shutil.rmtree(final, ignore_errors=True)
            raise
        out = ArchivedPath(final)
        out.snapshot = snapshot
        return out
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
