"""Cheap change signature of the files one annotation scope reads (holder wake gate, display trust). [daveey]

The holder used to probe every demanded scope every 2 s and re-read it every 20 s even when nothing moved
(t_01a1db1387f61a98 phase 2). It now sleeps on a scope while this signature is unchanged since it last verified it.
The signature covers every SQLite file the counts and the narrow key read (hub replica, boards, sync shadows, the
profile's ``state.db``, the origin ledger) plus the scope's mutation cue file:

* ``stat`` (inode, size, mtime in ns) of the database and its ``-wal``;
* the database header's file change counter (bytes 24-28: bumped by every write transaction in rollback mode);
* the WAL-index header at the start of ``-shm`` (its change counter moves on every WAL commit).

The two counters close the gap a coarse mtime leaves: a second commit in the same clock tick that does not grow the
file. A torn ``-shm`` read can only differ (an extra wake), never hide a later commit for longer than one tick.
Inputs it cannot see (a plugin's code, time-derived verdicts) are bounded by the holder's due-time fallback.
Any error other than a missing file raises; callers treat that as "moved".
"""
from __future__ import annotations

import hashlib
import json
import os


def _stat(path: str) -> list:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return [path, None]
    return [path, st.st_ino, st.st_size, st.st_mtime_ns]


def _head(path: str, start: int, end: int) -> str | None:
    try:
        with open(path, "rb") as f:
            f.seek(start)
            return f.read(end - start).hex()
    except FileNotFoundError:
        return None


def signature(scope: tuple[str, ...], dbs: list[str], files: list[str]) -> str:
    body: list = [list(scope)]
    for db in dbs:
        body.append([_stat(db), _stat(db + "-wal"), _head(db, 24, 28), _head(db + "-shm", 0, 48)])
    body.extend(_stat(f) for f in files)
    return hashlib.sha256(json.dumps(body, separators=(",", ":")).encode()).hexdigest()
