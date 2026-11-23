"""Adopt foreign-owned entries in a terminal scratch tree before it is archived. [daveey]

worker-a2 (2026-11-23): two finished cards' root-filesystem builds (88 GiB) were
kept forever and stopped the worker claiming cards (``worker_disk_below_floor``).
The builds ran under sudo, so the trees held root-owned files and directories:
the unprivileged archive scan hit ``PermissionError`` on every hourly retry, and
even a readable root-owned directory cannot be emptied by the worker user, so
removal after a successful archive would leave a half-deleted tree.

When the worker user has passwordless sudo (``sudo -n``), :func:`adopt` records
every entry not owned by the worker user (relative path, uid, gid, mode, type)
into :data:`RECORD_NAME` at the workspace root, then ``chown -R -P`` the tree to
the worker user. The record is written before the chown and is archived with the
payload, so the original ownership is restorable. Nothing else changes: the
normal archive, verification and guarded removal run unprivileged afterwards.

Refused (``ArchiveRefused``; the tree is kept): any mount point at or below the
workspace (a chroot with a bind-mounted /dev or host directory must never be
chowned through), a failed find/tee/chown, or an unparseable earlier record.
Without passwordless sudo, or when already running as root, it does nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

RECORD_NAME = ".hermes-foreign-ownership-v1.json"
_TIMEOUT = 1800
_run = subprocess.run  # tests replace it


def _sudo(*args: str, input: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = _run(["sudo", "-n", *args], input=input, capture_output=True, timeout=_TIMEOUT)
    if check and proc.returncode != 0:
        from hermes_cli.kanban_scratch_archive import ArchiveRefused
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()[:300]
        raise ArchiveRefused(f"foreign ownership adoption: {args[0]} failed (rc={proc.returncode}): {err}")
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


def adopt(workspace: Path) -> list[dict]:
    """Record then chown every foreign-owned entry of *workspace*; return the new records (may be empty)."""
    from hermes_cli.kanban_scratch_archive import ArchiveRefused
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
    probe = _sudo("find", root, "-xdev", "!", "-uid", str(uid), "-print", "-quit")
    if not probe.stdout.strip():
        return []
    mounts = mounts_below(Path(root))
    if mounts:
        raise ArchiveRefused(f"foreign ownership adoption: mount point(s) inside workspace: {mounts[:3]}")
    listing = _sudo("find", root, "-xdev", "!", "-uid", str(uid), "-printf", "%U %G %m %y %P\\0")
    rows = _parse(listing.stdout)
    record = Path(root) / RECORD_NAME
    earlier: list[dict] = []
    read = _sudo("cat", "--", str(record), check=False)
    if read.returncode == 0:
        try:
            earlier = json.loads(read.stdout)["entries"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ArchiveRefused(f"foreign ownership adoption: unreadable {RECORD_NAME}: {exc!r}") from exc
    known = {e["path"] for e in earlier}
    merged = earlier + [r for r in rows if r["path"] not in known and r["path"] != RECORD_NAME]
    body = json.dumps({"version": 1, "adopted_to": [uid, gid], "entries": merged}, indent=1).encode() + b"\n"
    _sudo("tee", "--", str(record), input=body)
    _sudo("chown", "-R", "-P", "--no-dereference", f"{uid}:{gid}", "--", root)
    return rows
