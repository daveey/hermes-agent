"""Bounded directory-membership cache for polling signatures.

Children's contents are not cached. Callers must still stat files/subdirectories.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict

_MAX_DIRECTORIES = 4096
_cache: OrderedDict[str, tuple] = OrderedDict()
_lock = threading.Lock()


def directory_snapshot(root) -> tuple[int, tuple]:
    """Return (mtime_ns, ((name, is_dir), ...)), listing only changed directories.

    A recent timestamp cannot certify membership on a coarse filesystem. Stat
    before listing, and leave such reads uncached until the clock has settled.
    Symlink target types are checked even when the link's parent has not moved.
    """
    from utils import mtime_is_racy

    path = os.path.abspath(root)
    with _lock:
        st = os.stat(path)
        key = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)
        memo = _cache.get(path)
        if memo is not None and memo[0] == key:
            links = tuple((name, os.path.isdir(os.path.join(path, name))) for name, _ in memo[2])
            if links == memo[2]:
                _cache.move_to_end(path)
                return st.st_mtime_ns, memo[1]
        _cache.pop(path, None)
        entries, links = [], []
        with os.scandir(path) as scan:
            for entry in scan:
                try:
                    row = (entry.name, entry.is_dir())
                    entries.append(row)
                    if entry.is_symlink():
                        links.append(row)
                except OSError:
                    continue
        result = tuple(entries)
        if not mtime_is_racy(st.st_mtime_ns):
            _cache[path] = (key, result, tuple(links))
            while len(_cache) > _MAX_DIRECTORIES:
                _cache.popitem(last=False)
        return st.st_mtime_ns, result
