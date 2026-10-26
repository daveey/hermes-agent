"""Read-only observations, NOT the design's normative live/apply schema.

``schema`` is hermes-retention-observation-v1 (or observation-plan-v1), never
RetentionPlan v1. Paths are base64 of filesystem bytes, relative to workspace;
stat device/inode and nanosecond times are decimal strings. Counts include the
candidate directory. Allocation is st_blocks * 512, deduplicated by dev/inode
(including directories), NOT reclaimable/exclusive bytes. Unknown totals are
None; measured_partial_allocated_bytes describes only successfully statted
objects. No file contents, Git commands, or write operations are used.

This is a bounded, best-effort observation, not an atomic snapshot or proof of
absence of Git/writers. Linux fdinfo mount IDs detect same-device bind mounts;
unavailable mount identity fails closed. Atime may change. Limits bound entries,
depth and memory, not time spent in a blocking kernel/filesystem operation.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
import stat

SCHEMA = "hermes-retention-observation-v1"
PLAN_SCHEMA = "hermes-retention-observation-plan-v1"
MAX_DEPTH = 128
MAX_BYTES = (1 << 53) - 1


def _b64(path: bytes) -> str:
    return base64.b64encode(path).decode("ascii")


def _relative(value: str) -> bytes:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("expected an exact nonempty POSIX relative path")
    if any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError("absolute, empty and dot path components are forbidden")
    raw = os.fsencode(value)
    if len(raw) > 4096 or len(raw.split(b"/")) > MAX_DEPTH:
        raise ValueError("candidate path exceeds traversal limits")
    return raw


def _flags() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("no-follow directory descriptors unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _mount(fd: int) -> str:
    # Kernel-owned descriptor metadata; never open candidate file contents.
    with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as stream:
        for line in stream:
            if line.startswith("mnt_id:"):
                value = line.split()[1]
                if value.isdecimal():
                    return value
    raise OSError("mount identity unavailable")


def _identity(s) -> tuple:
    return s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def _open_workspace(path: bytes) -> int:
    fd = os.open(b"/", _flags())
    try:
        for part in path.split(b"/")[1:]:
            new = os.open(part, _flags(), dir_fd=fd)
            os.close(fd)
            fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise


def _git_reason(fd: int) -> str | None:
    names = set()
    for name in (b".git", b"HEAD", b"objects", b"refs", b"reftable"):
        try:
            os.stat(name, dir_fd=fd, follow_symlinks=False)
            names.add(name)
        except FileNotFoundError:
            continue
    if b".git" in names:
        return "git_metadata"
    if {b"HEAD", b"objects"} <= names and names & {b"refs", b"reftable"}:
        return "possible_bare_repository"
    return None


def _record(path: bytes, s) -> dict:
    kind = "other"
    for predicate, label in ((stat.S_ISDIR, "directory"), (stat.S_ISREG, "file"),
                             (stat.S_ISLNK, "symlink")):
        if predicate(s.st_mode):
            kind = label
            break
    blocks = getattr(s, "st_blocks", None)
    allocated = blocks * 512 if blocks is not None and blocks >= 0 else None
    if allocated is not None and allocated > MAX_BYTES:
        allocated = None
    return {"path_b64": _b64(path), "type": kind, "mode": s.st_mode,
            "uid": s.st_uid, "gid": s.st_gid, "size": str(s.st_size),
            "device": str(s.st_dev), "inode": str(s.st_ino), "nlink": s.st_nlink,
            "mtime_ns": str(s.st_mtime_ns), "ctime_ns": str(s.st_ctime_ns),
            "allocated_bytes": allocated, "decision": "retain",
            "reason_codes": ["observation_only"], "deletion_authorized": False}


class _Scan:
    def __init__(self, limit: int):
        self.limit = limit
        self.records = []
        self.errors = []
        self.directories = set()
        self.attempts = 0

    def error(self, path: bytes, code: str, exc=None):
        self.errors.append({"path_b64": _b64(path), "code": code,
                            "errno": getattr(exc, "errno", None)})

    def walk(self, parent: int, name: bytes, path: bytes, mount: str, depth: int):
        self.attempts += 1
        try:
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            record = _record(path, before)
            self.records.append(record)
            if record["type"] != "directory":
                if record["type"] != "file":
                    self.error(path, record["type"] + "_retained")
                else:
                    self.file_identity(parent, name, path, before, mount)
                return
            fd = os.open(name, _flags(), dir_fd=parent)
            try:
                if _identity(before) != _identity(os.fstat(fd)):
                    self.error(path, "identity_changed")
                    return
                self.directory(fd, path, before, mount, depth)
                after = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if _identity(before) != _identity(after):
                    self.error(path, "identity_changed")
            finally:
                os.close(fd)
        except OSError as exc:
            self.error(path, "filesystem_error", exc)

    def file_identity(self, parent, name, path, before, mount):
        if not hasattr(os, "O_PATH"):
            self.error(path, "mount_identity_unavailable")
            return
        fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            if _identity(before) != _identity(os.fstat(fd)):
                self.error(path, "identity_changed")
            if _mount(fd) != mount:
                self.error(path, "mount_boundary")
        finally:
            os.close(fd)

    def directory(self, fd: int, path: bytes, before, mount: str, depth: int):
        if _mount(fd) != mount:
            self.error(path, "mount_boundary")
            return
        key = (before.st_dev, before.st_ino)
        if key in self.directories:
            self.error(path, "directory_alias")
            return
        self.directories.add(key)
        reason = "git_metadata" if path.split(b"/")[-1] == b".git" else _git_reason(fd)
        if reason:
            self.error(path, reason)
            return
        if depth >= MAX_DEPTH:
            self.error(path, "depth_limit")
            return
        with os.scandir(fd) as entries:
            for entry in entries:
                if self.attempts >= self.limit:
                    self.error(path, "entry_limit")
                    break
                self.walk(fd, os.fsencode(entry.name), path + b"/" + os.fsencode(entry.name),
                          mount, depth + 1)
        if _identity(before) != _identity(os.fstat(fd)):
            self.error(path, "directory_changed")


def _totals(records: list[dict], complete: bool) -> dict:
    allocations = {}
    known = True
    for record in records:
        key = (record["device"], record["inode"])
        value = record["allocated_bytes"]
        if key in allocations and allocations[key] != value:
            allocations[key] = None
        else:
            allocations.setdefault(key, value)
    measured = sum(v for v in allocations.values() if v is not None)
    known = all(v is not None for v in allocations.values()) and measured <= MAX_BYTES
    total = measured if complete and known else None
    return {"observed_entries": len(records), "retained_entries": len(records),
            "unique_observed_inodes": len(allocations), "allocated_bytes": total,
            "retained_allocated_bytes": total,
            "measured_partial_allocated_bytes": measured if measured <= MAX_BYTES else None,
            "unknown_bytes": 0 if total is not None else None,
            "eligible_bytes": 0, "exclusive_bytes_lower_bound": 0}


def inventory(workspace: Path, relative_path: str, max_entries=100000) -> dict:
    """Observe one exact directory beneath an explicit absolute workspace.

    Invalid arguments raise ValueError; filesystem/coverage failures are returned
    as errors with scan_complete=False. A missing root has no invented identity.
    """
    raw = _relative(relative_path)
    if type(max_entries) is not int or not 1 <= max_entries <= 100000:
        raise ValueError("max_entries must be an integer in [1, 100000]")
    absolute = os.fsencode(workspace)
    if not absolute.startswith(b"/") or absolute == b"/":
        raise ValueError("workspace must be an explicit absolute non-root directory")
    if any(p in (b"", b".", b"..") for p in absolute.split(b"/")[1:]):
        raise ValueError("workspace must have exact normalized components")
    scan = _Scan(max_entries)
    workspace_identity = None
    fd = None
    try:
        fd = _open_workspace(absolute)
        if b".git" in absolute.split(b"/") or _git_reason(fd) == "possible_bare_repository":
            raise OSError("Git metadata workspace retained")
        s = os.fstat(fd)
        workspace_identity = {"device": str(s.st_dev), "inode": str(s.st_ino)}
        mount = _mount(fd)
        parts = raw.split(b"/")
        for index, part in enumerate(parts[:-1]):
            if part == b".git" or (index > 0 and _git_reason(fd)):
                raise OSError("Git ancestor retained")
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            child = os.open(part, _flags(), dir_fd=fd)
            os.close(fd)
            fd = child
            if _identity(before) != _identity(os.fstat(fd)) or _mount(fd) != mount:
                raise OSError("ancestor identity or mount changed")
        # The workspace may itself be a checkout; only candidate ancestors below
        # it are excluded. No implicit expansion to adjacent workspace content.
        if len(parts) > 1 and _git_reason(fd):
            raise OSError("Git ancestor retained")
        if parts[-1] == b".git":
            scan.error(raw, "git_metadata")
        scan.walk(fd, parts[-1], raw, mount, 0)
        if scan.records and scan.records[0]["type"] != "directory":
            scan.error(raw, "candidate_not_directory")
    except OSError as exc:
        scan.error(raw, "root_or_ancestor_unavailable", exc)
    finally:
        if fd is not None:
            os.close(fd)
    complete = not scan.errors
    return {"schema": SCHEMA, "mode": "observation-only", "apply_eligible": False,
            "deletion_authorized": False, "decision": "retain",
            "workspace_b64": _b64(absolute), "workspace_identity": workspace_identity,
            "relative_path_b64": _b64(raw), "scan_complete": complete,
            "records": sorted(scan.records, key=lambda r: base64.b64decode(r["path_b64"])),
            "errors": scan.errors, "totals": _totals(scan.records, complete)}


def _overlap(a: bytes, b: bytes) -> bool:
    return a == b or a.startswith(b + b"/") or b.startswith(a + b"/")


def plan(inventories: list[dict]) -> dict:
    """Combine observations, refusing overlap/alias roots; never authorize apply.

    Inputs are observations from inventory(), not trusted authority or live-plan
    manifests. Recompute deduplicated allocation across disjoint candidates.
    """
    paths, roots, records, errors = [], set(), [], []
    workspace_paths = {}
    observed_directories = set()
    for item in inventories:
        if (item.get("schema") != SCHEMA or item.get("mode") != "observation-only"
                or item.get("deletion_authorized") is not False
                or item.get("apply_eligible") is not False):
            raise ValueError("expected observation-only inventory")
        workspace = base64.b64decode(item["workspace_b64"], validate=True)
        relative = base64.b64decode(item["relative_path_b64"], validate=True)
        _relative(os.fsdecode(relative))
        path = workspace + b"/" + relative
        if any(_overlap(path, p) for p in paths):
            raise ValueError("overlapping candidate paths")
        paths.append(path)
        identity = item.get("workspace_identity")
        if identity is not None:
            workspace_key = (identity["device"], identity["inode"])
            prior = workspace_paths.setdefault(workspace_key, [])
            if any(_overlap(relative, p) for p in prior):
                raise ValueError("overlapping candidate paths through workspace aliases")
            prior.append(relative)
        root = next((r for r in item["records"] if r["path_b64"] == item["relative_path_b64"]), None)
        if root:
            key = (root["device"], root["inode"])
            if key in roots:
                raise ValueError("duplicate device/inode candidate roots")
            if key in observed_directories:
                raise ValueError("overlapping candidate directory identities")
            new_directories = {(r["device"], r["inode"]) for r in item["records"]
                               if r["type"] == "directory"}
            if roots & new_directories:
                raise ValueError("overlapping candidate directory identities")
            roots.add(key)
            observed_directories.update(new_directories)
        records.extend(item["records"])
        errors.extend(item["errors"])
    complete = all(i["scan_complete"] for i in inventories) and not errors
    return {"schema": PLAN_SCHEMA, "mode": "observation-only", "apply_eligible": False,
            "deletion_authorized": False, "decision": "retain",
            "candidate_count": len(inventories), "scan_complete": complete,
            "errors": errors, "totals": _totals(records, complete)}
