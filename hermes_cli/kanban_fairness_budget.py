"""Capture-wide logical-byte ceiling, including metadata and temporary files.

Only writer/startup code calls this module; enqueue never takes a file lock or
walks the capture. A failed partial append remains unsealed. No evidence is deleted.
Cooperating processes serialize check + IO under one root lock. External writes
are counted on the next write but cannot be prevented by this advisory protocol.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path

NAME = "capture-budget.json"
LIMIT = 512 * 1024 * 1024


class BudgetExceeded(ValueError):
    """The capture cannot accept this write, including its temporary-file peak."""


@contextmanager
def _locked(root):
    try:
        import fcntl
    except ImportError as exc:
        raise ValueError("capture budget requires advisory file locking") from exc
    if (root / ".capture-budget.lock").is_symlink():
        raise ValueError("budget lock must not be a symlink")
    with (root / ".capture-budget.lock").open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _used(root):
    total, count = 0, 0

    def walk(directory, depth=0):
        nonlocal total, count
        if depth > 4:
            raise ValueError("capture directory nesting exceeded")
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > 100000:
                    raise BudgetExceeded("capture file count exceeded")
                if entry.is_symlink():
                    raise ValueError("capture must not contain symlinks")
                if entry.is_dir(follow_symlinks=False):
                    walk(entry.path, depth + 1)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                else:
                    raise ValueError("capture must contain only regular files")
    walk(root)
    return total


def _limit(root):
    with (root / NAME).open("rb") as stream:
        data = stream.read(257)
    if len(data) > 256:
        raise ValueError("invalid budget metadata")
    value = json.loads(data)
    if (not isinstance(value, dict) or set(value) != {"version", "max_bytes"}
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["max_bytes"]) is not int or not 0 < value["max_bytes"] <= LIMIT):
        raise ValueError("invalid budget metadata")
    return value["max_bytes"]


def initialize(root: Path, maximum=None):
    requested = maximum
    maximum = LIMIT if maximum is None else maximum
    if type(maximum) is not int or not 0 < maximum <= LIMIT:
        raise ValueError("invalid capture budget")
    data = json.dumps({"version": 1, "max_bytes": maximum}).encode()
    if len(data) > maximum:
        raise BudgetExceeded("capture budget cannot hold metadata")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("capture root must not be a symlink")
    with _locked(root):
        if (root / NAME).exists():
            if _limit(root) != maximum and requested is not None:
                raise ValueError("capture budget cannot change")
            return
        if _used(root) + len(data) > maximum:
            raise BudgetExceeded("capture already exceeds budget")
        _write(root / NAME, data)


def _write(path, data):
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write(path, data):
    """Route every segment/lifecycle write through its explicitly initialized root."""
    parent = path.parent
    root = parent.parent.parent if parent.parent.name == "runtime" else parent
    if not (root / NAME).exists():
        if root != parent:
            raise ValueError("runtime capture budget missing")
        _write(path, data)
        return
    with _locked(root):
        if _used(root) + len(data) > _limit(root):
            raise BudgetExceeded("capture storage bound exceeded")
        _write(path, data)

