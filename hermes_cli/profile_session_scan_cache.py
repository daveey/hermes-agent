"""Bounded, change-keyed DB projections for aggregate session polls (no TTL).

The caller holds the scan lock for *all* operations. Observer connections never
hold a read transaction, and only observe data_version: every writer (including
writers in this process) uses another connection. Reopening an observer discards
its projections, since data_version is comparable only on the same connection.
"""
import copy
import sqlite3
from collections import OrderedDict
from pathlib import Path


def _identity(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino


class ProfileSessionScanCache:
    def __init__(self, max_profiles=64, max_queries=16):
        self.max_profiles = max_profiles
        self.max_queries = max_queries
        self._profiles = OrderedDict()

    def _drop(self, path):
        entry = self._profiles.pop(path, None)
        if entry is not None:
            entry[1].close()

    def clear(self):
        for path in list(self._profiles):
            self._drop(path)

    def _observe(self, path):
        identity = _identity(path)
        entry = self._profiles.get(path)
        if entry is not None and entry[0] != identity:
            self._drop(path)
            entry = None
        if entry is None:
            conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                   check_same_thread=False, isolation_level=None, timeout=0)
            entry = (identity, conn, OrderedDict())
            self._profiles[path] = entry
        self._profiles.move_to_end(path)
        while len(self._profiles) > self.max_profiles:
            self._drop(next(iter(self._profiles)))
        version = entry[1].execute('PRAGMA data_version').fetchone()[0]
        # Include identity to notice a file replacement between the two probes.
        return entry, (identity, version)

    def read(self, home, key, scan, clone=copy.deepcopy):
        """``scan()``, or a ``clone`` of its stored result while ``home``'s state.db has not committed since.
        ``clone`` copies everything a caller may mutate, both ways (default: a deep copy)."""
        if home is None:
            return scan()
        path = (Path(home) / 'state.db').absolute()
        try:
            entry, before = self._observe(path)
        except (OSError, sqlite3.Error):
            self._drop(path)
            return scan()  # preserve the normal missing/corrupt/retry error path
        queries = entry[2]
        cached = queries.get(key)
        if cached is not None and cached[0] == before:
            queries.move_to_end(key)
            return clone(cached[1])
        # Invalidate before scanning, including when a failed scan returns None.
        for old in [k for k, value in queries.items() if value[0] != before]:
            del queries[old]
        result = scan()
        try:
            after_entry, after = self._observe(path)
        except (OSError, sqlite3.Error):
            self._drop(path)
            return result
        if result is not None and after_entry is entry and before == after:
            queries[key] = (before, clone(result))
            queries.move_to_end(key)
            while len(queries) > self.max_queries:
                queries.popitem(last=False)
        return result
