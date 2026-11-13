"""Git shapes the scratch archive used to refuse forever, archived instead. [daveey]

worker-2 (2026-11-13) kept 54 aged scratch trees (37.8 GiB) because each retry of
``kanban_scratch_archive`` failed the same way. This module holds the pieces that
let those shapes archive, while anything still unsafe keeps refusing:

* :func:`select_unpushed` picks the refs ``commits.bundle`` carries. A ref whose
  tip the remote already holds is left out: one contained in ``refs/remotes``
  (as before) or one whose tip came from a promisor pack (``rev-list
  --exclude-promisor-objects``), such as the tags a ``blob:none`` clone fetched,
  whose bundle needed blobs that were never local (``fatal: unable to read``).
  A remaining ref is bundled only when every object it adds is local. A ref
  that needs objects the promisor remote vouches for is recorded and left to the
  complete payload (its local objects are in ``.git``); a ref needing objects
  nothing vouches for refuses with reason ``unrecoverable_objects``.
* :func:`parse_gitlink` reads a ``.git`` gitlink file (linked worktree or
  submodule). A gitdir inside the scanned tree is archived as a nested repo; a
  gitdir outside it gets the worktree diff plus HEAD/branch, read through
  :func:`private_index` so git never writes there. A gitdir that is missing,
  empty, or inside the tree but not a real directory there still refuses.
"""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

UNRECOVERABLE = "unrecoverable_objects"


def _refused(message: str) -> Exception:
    from hermes_cli.kanban_scratch_archive import ArchiveRefused
    return ArchiveRefused(message)


def parse_gitlink(root: Path, rel: str, head: bytes, found: dict[str, os.stat_result]) -> tuple[str, bool]:
    """(gitdir as written, inside *root*) for the gitlink at ``<rel>/.git`` whose first bytes are *head*.

    Git resolves a relative pointer against the directory holding the ``.git``
    file. Containment is lexical on absolute, already symlink-free paths (the
    scan opens every component without following links), and an inside target
    must be a directory the scan itself recorded at every component, so a
    pointer through a symlink inside the tree is refused rather than followed.
    """
    directory = root if rel == "." else root / rel
    where = f"linked worktree or submodule at {directory / '.git'}"
    line = head.lstrip()[len(b"gitdir:"):].split(b"\n", 1)[0].strip()
    if not line:
        raise _refused(f"{where}: empty gitdir pointer")
    pointer = os.fsdecode(line)
    top = os.path.normpath(os.path.abspath(root))
    target = os.path.normpath(os.path.join(os.path.abspath(directory), pointer))
    if target == top or target.startswith(top.rstrip(os.sep) + os.sep):
        sub = os.path.relpath(target, top).replace(os.sep, "/")
        parts = [] if sub == "." else sub.split("/")
        for i in range(len(parts) + 1):
            st = found.get("/".join(parts[:i]) or ".")
            if st is None or not stat.S_ISDIR(st.st_mode):
                raise _refused(f"{where}: gitdir {pointer} is not a directory in the workspace")
        return pointer, True
    if not os.path.isdir(target):  # reads outside the tree only to look; never writes there
        raise _refused(f"{where}: gitdir {pointer} is missing")
    return pointer, False


def _commit_tips(repo: Path, env: dict, git, head: str | None) -> tuple[dict[str, str], set[str], list[str]]:
    """({local ref: peeled commit}, remote-tracking tips, refs needing a per-ref walk)."""
    fmt = "%(objectname) %(objecttype) %(*objectname) %(*objecttype) %(refname)"
    out = git(repo, env, "for-each-ref", f"--format={fmt}").stdout.decode("utf-8", "replace")
    tips: dict[str, str] = {}
    remote: set[str] = set()
    nested: list[str] = []
    for line in out.splitlines():
        parts = line.split(" ", 4)
        if len(parts) != 5:
            continue
        oid, kind, peeled, peeled_kind, ref = parts
        if ref.startswith("refs/remotes/"):
            remote.add(oid)
        elif kind == "commit":
            tips[ref] = oid
        elif kind == "tag" and peeled_kind == "commit":
            tips[ref] = peeled
        elif kind == "tag" and peeled_kind == "tag":
            nested.append(ref)  # tag of a tag: walked on its own below
        # a ref to a tree or blob names no commit, so nothing of it is unpushed history
    if head:
        tips["HEAD"] = head
    return tips, remote, nested


def _rev_list(repo: Path, env: dict, git, args: list[str], positive: list[str], negative: set[str],
              ok=(0,)):
    stdin = "".join(f"{o}\n" for o in positive) + "".join(f"^{o}\n" for o in sorted(negative))
    return git(repo, env, "rev-list", *args, "--stdin", ok=ok, stdin=stdin.encode())


def _missing(proc) -> list[str]:
    return [line[1:].split()[0] for line in proc.stdout.decode("ascii", "replace").splitlines()
            if line.startswith("?")]


def select_unpushed(repo: Path, env: dict, git, head: str | None, *, partial: bool) -> tuple[list[str], dict]:
    """(refs to bundle, meta additions); raises ``ArchiveRefused`` (``unrecoverable_objects``).

    Every walk is offline and over local objects only (the archive env forbids
    lazy fetches). Commit walks only, except the object walks that prove a
    bundle can be written, which a ref no longer needing a bundle never pays.
    """
    tips, remote, nested = _commit_tips(repo, env, git, head)
    local = _rev_list(repo, env, git, ["--exclude-promisor-objects"], sorted(set(tips.values())), remote)
    unpushed = {line[:40] for line in local.stdout.decode("ascii", "replace").split()}
    refs = [r for r, oid in tips.items() if oid in unpushed]
    for ref in nested:
        walk = git(repo, env, "rev-list", "-n1", "--exclude-promisor-objects", ref, "--not", "--remotes")
        if walk.stdout.strip():
            refs.append(ref)
    refs.sort(key=lambda r: (r != "HEAD", r))
    if not refs:
        return [], {}
    if not _missing(_rev_list(repo, env, git, ["--objects", "--missing=print"], refs, remote)):
        return refs, {}
    bundle, promised = [], []
    for ref in refs:
        gone = _missing(_rev_list(repo, env, git, ["--objects", "--missing=print"], [ref], remote))
        if not gone:
            bundle.append(ref)
            continue
        vouched = partial and not _rev_list(repo, env, git, ["--objects", "--missing=allow-promisor"], [ref],
                                            remote, ok=(0, 1, 128)).returncode
        if not vouched:
            raise _refused(f"{UNRECOVERABLE}: {ref} in {repo} needs {len(gone)} missing object(s) no "
                           f"promisor remote vouches for (e.g. {gone[0]})")
        promised.append(ref)
    return bundle, {"promised_refs": promised}


@contextmanager
def private_index(repo: Path, env: dict, git, scratch: Path) -> Iterator[dict]:
    """*env* with ``GIT_INDEX_FILE`` on a private copy of the worktree's index, under *scratch*.

    ``git diff`` refreshes and rewrites a stat-dirty index, and that index lives
    in the gitdir; for a gitdir outside the scratch tree every such write would
    land in someone else's repository. The copy takes them all instead.
    """
    source = git(repo, env, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout
    source = Path(os.fsdecode(source.rstrip(b"\r\n")))
    with tempfile.TemporaryDirectory(prefix=".index-", dir=scratch) as tmp:
        copy = Path(tmp) / "index"
        if source.is_file():
            shutil.copyfile(source, copy)
        yield dict(env, GIT_INDEX_FILE=str(copy), GIT_OPTIONAL_LOCKS="0")
