"""Stdlib-only inventory probe, also sent verbatim to the hub; never extracts tar files.

Archives contain the workspace contents rooted at '.', including standalone .git
metadata. Linked worktrees, alternates, links and special files fail closed: their
external dependencies are not portable merely because their path was archived.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tarfile


MAX_ENTRIES = 100000


def file_hash(stream):
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def entry(name, mode, directory, digest):
    parts = PurePosixPath(name).parts
    if ((".git" in parts and not directory and parts[-1] == ".git")
            or name.endswith(("/objects/info/alternates", "/.git/commondir"))
            or name == ".git/commondir"):
        raise ValueError("external Git metadata")
    return [name, mode & 0o777, "dir" if directory else "file", digest]


def inventory_hash(entries):
    if len(entries) > MAX_ENTRIES:
        raise ValueError("inventory too large")
    return hashlib.sha256(json.dumps(sorted(entries), separators=(",", ":")).encode()).hexdigest()


def tree_inventory(root):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("not a plain directory")
    entries = [entry(".", root.stat().st_mode, True, "")]

    def failed(error):
        raise error

    for parent, dirs, files in os.walk(root, onerror=failed, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            mode = path.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError("unsupported filesystem entry")
            digest = ""
            if stat.S_ISREG(mode):
                with path.open("rb") as stream:
                    digest = file_hash(stream)
            entries.append(entry(path.relative_to(root).as_posix(), mode, stat.S_ISDIR(mode), digest))
            if len(entries) > MAX_ENTRIES:
                raise ValueError("inventory too large")
    return inventory_hash(entries)


def archive_inventory(path, expected):
    entries, names = [], set()
    with open(path, "rb") as stream:
        before = os.fstat(stream.fileno())
        if file_hash(stream) != expected:
            raise ValueError("archive checksum mismatch")
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r|*") as archive:
            for member in archive:
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts or not (member.isdir() or member.isfile()):
                    raise ValueError("unsupported archive entry")
                key = name.as_posix()
                if key in names or len(names) >= MAX_ENTRIES:
                    raise ValueError("duplicate or oversized inventory")
                names.add(key)
                digest = ""
                if member.isfile():
                    with archive.extractfile(member) as source:
                        digest = file_hash(source)
                entries.append(entry(key, member.mode, member.isdir(), digest))
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("archive changed while reading")
    return inventory_hash(entries)


if __name__ == "__main__":
    try:
        result = tree_inventory(sys.argv[2]) if sys.argv[1] == "tree" else archive_inventory(sys.argv[2], sys.argv[3])
        print(result)
    except (OSError, ValueError, tarfile.TarError):
        sys.exit(1)
