"""SQLite's own proof that a board's main file moved because of a WAL checkpoint. [daveey]

A kept read-only connection survives a checkpoint by design: the wal-index tells it which pages
to read from where. It does not survive a raw overwrite of the main file, which bypasses the WAL
and leaves its page cache describing bytes that are gone. Both change the main file's mtime, so
``BoardWatch`` used to reopen on either (about 3 ms of schema parsing per board per checkpoint on
the MacBook gateway, 2026-11-22). This module reads the wal-index header from ``<db>-shm`` and
answers one question: did a checkpoint run between two observations?

Layout (SQLite ``wal.c``, native byte order): two copies of the 48-byte ``WalIndexHdr``
(``isInit`` at 12, ``mxFrame`` at 16, ``aSalt`` at 32..40), then ``WalCkptInfo`` whose first
word, at offset 96, is ``nBackfill``: the number of WAL frames already copied into the main file.

Only a lock-safe ``pread`` touches the file: a plain open/close of ``-shm`` in a process that
holds connections cancels their POSIX locks (see :mod:`hermes_cli.sqlite_safe_read`).
"""
from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import NamedTuple, Optional

from hermes_cli.sqlite_safe_read import pread_lock_safe_stat

_HDR = 48
_LEN = 136  # two header copies + WalCkptInfo
_WORD = struct.Struct("=I")


class WalState(NamedTuple):
    shm: tuple[int, int]  # (st_dev, st_ino) of the -shm that was read
    salts: bytes  # aSalt[0..1]: one WAL generation; rewritten when the log restarts
    max_frame: int  # mxFrame: last valid frame of this generation
    backfilled: int  # nBackfill: frames of this generation already in the main file


def read(db_path: Path | str) -> Optional[WalState]:
    """The board's wal-index state, or None when it cannot be read with certainty (no ``-shm``:
    a rollback-journal database; a torn or uninitialised header; a platform without ``pread``).
    *db_path* must be the path SQLite opened (symlinks resolved): it names the sidecar by that."""
    if not hasattr(os, "pread"):
        return None
    got = pread_lock_safe_stat(os.fspath(db_path) + "-shm", 0, _LEN)
    if got is None or len(got[0]) != _LEN:
        return None
    raw, st = got
    first = raw[:_HDR]
    # A writer updates the copies one after the other: unequal copies are a header in motion.
    if first != raw[_HDR:2 * _HDR] or first[12] != 1:
        return None
    return WalState((st.st_dev, st.st_ino), first[32:40], _WORD.unpack_from(first, 16)[0],
                    _WORD.unpack_from(raw, 96)[0])


def checkpointed(before: Optional[WalState], after: Optional[WalState]) -> bool:
    """True only when *after* proves a checkpoint copied frames into the main file since *before*.

    Same WAL generation: ``nBackfill`` advanced. A later generation: the log restarts only once
    every frame is backfilled, so frames outstanding at *before* were checkpointed after it, and
    any frame backfilled in the new generation was too. A restart from a fully backfilled baseline
    into a generation with nothing backfilled proves nothing. Anything unreadable is doubt."""
    if before is None or after is None or before.shm != after.shm:
        return False
    if before.salts == after.salts:
        return after.backfilled > before.backfilled
    return before.backfilled < before.max_frame or after.backfilled > 0
