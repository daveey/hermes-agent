"""Adopt foreign-owned entries in a terminal scratch tree before it is archived. [daveey]

worker-a2 (2026-11-23): two finished cards' root-filesystem builds (88 GiB) were
kept forever and stopped the worker claiming cards (``worker_disk_below_floor``).
The builds ran under sudo, so the trees held root-owned files and directories:
the unprivileged archive scan hit ``PermissionError`` on every hourly retry, and
even a readable root-owned directory cannot be emptied by the worker user, so
removal after a successful archive would leave a half-deleted tree.

When the worker user has passwordless sudo (``sudo -n``), :func:`adopt` records
every entry whose uid or gid is not the worker's (relative path, uid, gid, mode,
type) into :data:`RECORD_NAME` at the workspace root, then gives those entries
(and only those) to the worker user with ``find -xdev ... -exec chown -h`` and
adds owner read/write (and search, for directories) so the unprivileged archive
and removal can finish. The record is in place before any owner or mode changes
and is archived with the payload, so original owners and modes are restorable.
The normal archive, verification and guarded removal then run unchanged.

The caller's *owner_check* runs immediately before the first change: a card
resumed since GC selected the tree is never touched. Refused (``ArchiveRefused``;
the tree is kept unchanged): the owner check fails, any mount point at or below
the workspace, a foreign-owned regular file with more than one hard link (chown
acts on the inode, which may live outside the tree), a record path that is not a
regular file (a planted symlink must never be written through as root), an
unparseable earlier record, or a failed find/cat/mv. A failure after the record
is written leaves the tree adopted in part with its record at the root; a later
pass merges new entries by path. Without passwordless sudo, or as root, nothing
changes. ``find -xdev`` never crosses into another filesystem.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

RECORD_NAME = ".hermes-foreign-ownership-v1.json"
_TIMEOUT = 1800
_run = subprocess.run  # tests replace it


def _refused(msg: str) -> Exception:
    from hermes_cli.kanban_scratch_archive import ArchiveRefused
    return ArchiveRefused(f"foreign ownership adoption: {msg}")


def _sudo(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = _run(["sudo", "-n", *args], capture_output=True, timeout=_TIMEOUT)
    if check and proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()[:300]
        raise _refused(f"{args[0]} failed (rc={proc.returncode}): {err}")
    return proc


def _unescape_mount(field: str) -> str:
    # /proc/self/mountinfo escapes space, tab, newline and backslash as \ooo.
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and i + 3 < len(field) and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def mounts_below(root: Path, mountinfo: str | None = None) -> list[str]:
    """Mount points equal to or below *root* (from /proc/self/mountinfo)."""
    if mountinfo is None:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as fh:
            mountinfo = fh.read()
    base = os.path.realpath(root)
    found = []
    for line in mountinfo.splitlines():
        parts = line.split(" ")
        if len(parts) < 5:
            continue
        point = _unescape_mount(parts[4])
        if point == base or point.startswith(base.rstrip("/") + "/"):
            found.append(point)
    return found


def _parse(out: bytes) -> list[dict]:
    rows = []
    for rec in out.split(b"\0"):
        if not rec:
            continue
        uid, gid, mode, kind, rel = rec.split(b" ", 4)
        rows.append({"path": rel.decode("utf-8", "surrogateescape") or ".", "uid": int(uid), "gid": int(gid),
                     "mode": int(mode, 8), "type": kind.decode()})
    return rows


def _earlier(root: str) -> list[dict]:
    record = os.path.join(root, RECORD_NAME)
    if _sudo("find", root, "-maxdepth", "1", "-name", RECORD_NAME, "!", "-type", "f", "-print", "-quit").stdout.strip():
        raise _refused(f"{RECORD_NAME} is not a regular file")
    read = _sudo("cat", "--", record, check=False)
    if read.returncode != 0:
        return []
    try:
        return list(json.loads(read.stdout)["entries"])
    except (ValueError, KeyError, TypeError) as exc:
        raise _refused(f"unreadable {RECORD_NAME}: {exc!r}") from exc


def _write_record(root: str, body: bytes) -> None:
    # Written by the worker user beside the workspace, then renamed in as root: rename replaces
    # whatever sits at the name and never writes through a link.
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(root)}.ownership-", dir=os.path.dirname(root))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        _sudo("mv", "-T", "-f", "--", tmp, os.path.join(root, RECORD_NAME))
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def _grant_owner_access(root: str, rows: list[dict]) -> None:
    # Now worker-owned, so no sudo: an entry the worker cannot read or (for a directory) empty
    # would refuse the scan or leave a detached tree behind. Parents come first (find pre-order).
    for row in rows:
        path = root if row["path"] == "." else os.path.join(root, row["path"])
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            continue
        need = stat.S_IRWXU if stat.S_ISDIR(st.st_mode) else stat.S_IRUSR | stat.S_IWUSR
        if st.st_mode & need != need:
            os.chmod(path, stat.S_IMODE(st.st_mode) | need)


def adopt(workspace: Path, owner_check: Optional[Callable[[], bool]] = None) -> list[dict]:
    """Record, then give the worker user every foreign-owned entry of *workspace*; return the new records."""
    if not sys.platform.startswith("linux") or not hasattr(os, "geteuid"):
        return []
    uid, gid = os.geteuid(), os.getegid()  # windows-footgun: ok -- Linux-only, hasattr guard above
    if uid == 0:
        return []
    try:
        if _run(["sudo", "-n", "true"], capture_output=True, timeout=30).returncode != 0:
            return []
    except (OSError, subprocess.SubprocessError):
        return []
    root = os.path.realpath(workspace)
    foreign = ["(", "!", "-uid", str(uid), "-o", "!", "-gid", str(gid), ")"]
    if not _sudo("find", root, "-xdev", *foreign, "-print", "-quit").stdout.strip():
        return []
    mounts = mounts_below(Path(root))
    if mounts:
        raise _refused(f"mount point(s) inside workspace: {mounts[:3]}")
    linked = _sudo("find", root, "-xdev", *foreign, "-type", "f", "-links", "+1", "-print", "-quit").stdout.strip()
    if linked:
        raise _refused(f"foreign-owned file with several hard links: {linked[:200]!r}")
    earlier = _earlier(root)
    if owner_check is not None and not owner_check():
        raise _refused("the workspace owner changed since it was selected; left untouched")
    rows = _parse(_sudo("find", root, "-xdev", *foreign, "-printf", "%U %G %m %y %P\\0").stdout)
    known = {e["path"] for e in earlier}
    merged = earlier + [r for r in rows if r["path"] not in known and r["path"] != RECORD_NAME]
    _write_record(root, json.dumps({"version": 1, "adopted_to": [uid, gid], "entries": merged},
                                   indent=1).encode() + b"\n")
    _sudo("find", root, "-xdev", *foreign, "-exec", "chown", "-h", f"{uid}:{gid}", "--", "{}", "+")
    _grant_owner_access(root, [r for r in rows if r["path"] != RECORD_NAME])
    return rows
