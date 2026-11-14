"""Caller-owned raw replica reader for annotation semantic keys. [daveey]

Never reads or populates the strict projector's process-global Rows cache. The
caller owns the connection lifecycle and must hold one read transaction; it must
also fence the connection against pathname replacement before borrowing it (the
normal cached_ro lifecycle does this). Errors are unkeyable, not empty snapshots.
"""
from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass

from hermes_cli import kanban_replica_rows as rr

_PROBE = ("SELECT count(*), count(seq), max(seq), total(seq), total(version), "
          "total(octet_length(data)) FROM replica WHERE coll='tasks'")
_SEQ_PROBE = "SELECT count(*), count(seq), max(seq), total(seq) FROM replica WHERE coll='tasks'"


@dataclass(frozen=True)
class ReplicaSnapshot:
    rows: rr.Rows
    gateway: str
    info_rows: tuple
    last_ok: float | None
    offline_since: float | None


def _file_fence(path) -> tuple:
    """File identity (main inode + WAL-index inode). Size/mtime are deliberately NOT part of it: every WAL
    checkpoint moves them, and a 19k-row full re-read costs seconds of CPU (measured on worker-6). Content
    changes under one identity are proven by the seq/probe totals, the strict reader's own protocol."""
    st = os.stat(path)
    try:
        shm = os.stat(str(path) + '-shm')
        sidecar = shm.st_dev, shm.st_ino
    except FileNotFoundError:
        sidecar = None
    return st.st_dev, st.st_ino, sidecar


def _clock(conn: sqlite3.Connection, key: str) -> float | None:
    row = conn.execute('SELECT v FROM meta WHERE k=?', (key,)).fetchone()
    if not row or row[0] in (None, ''):
        return None
    value = float(row[0])
    if not math.isfinite(value):
        raise ValueError('nonfinite replica clock')
    return value


class ReplicaReader:
    """One serialized caller's last raw view; no retained transaction or owned FD.

    Same-connection data_version and total_changes are raw fences, not semantic
    inputs. Any other commit advances the view by the seq delta protocol the
    strict reader (``rr.read``) trusts; probe totals that do not add up force a
    full read. A seq-preserving, total-preserving in-place rewrite is invisible
    here exactly as it is to the strict reader; this key is display-only and the
    holder's 20 s forced renewal bounds any such miss. A new main-file or WAL
    index inode, schema change or connection always reads in full.
    """

    def __init__(self):
        self._conn = None
        self._fence = None
        self._version = None
        self._probe = None
        self._seq_probe = None
        self._rows = None

    def clear(self) -> None:
        self.__init__()

    def read(self, conn: sqlite3.Connection, path) -> ReplicaSnapshot:
        if not conn.in_transaction:
            raise ValueError('replica read requires transaction')
        try:
            return self._read(conn, path)
        except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
            self.clear()
            raise

    def _read(self, conn: sqlite3.Connection, path) -> ReplicaSnapshot:
        fence = _file_fence(path)
        schema = conn.execute('PRAGMA schema_version').fetchone()[0]
        version = (conn.execute('PRAGMA data_version').fetchone()[0], conn.total_changes)
        columns = {r[1] for r in conn.execute('PRAGMA table_info(replica)')}
        if not {'seq', 'version'} <= columns:
            raise ValueError('unproven replica sequence schema')
        identity = str(path), fence, schema
        base = self._rows if self._conn is conn and self._fence == identity else None
        # hermes_hub stamps every replica task write with the server log's seq (upsert/insert) and deletes rows
        # outright, so (count, max seq, seq total) - a ~1.5 ms covering scan of replica_poll_metadata - moves on
        # every task change. Unmoved: the ~30 ms full probe (a 19k-row data scan on worker-6) is skipped.
        seq_probe = tuple(conn.execute(_SEQ_PROBE).fetchone())
        unchanged = base is not None and (version == self._version or seq_probe == self._seq_probe)
        probe = self._probe if unchanged else tuple(conn.execute(_PROBE).fetchone())
        if probe[0] != probe[1]:
            raise ValueError('unproven replica sequence')
        from hermes_cli.kanban_session_activity import _self_gateway
        gateway = _self_gateway(conn)
        if not gateway:
            raise ValueError('unknown replica gateway')
        infos = tuple(conn.execute("SELECT gw,data FROM replica WHERE coll='gateway' AND key='info' ORDER BY gw"))
        last_ok, offline = _clock(conn, 'last_ok'), _clock(conn, 'offline_since')
        rows = None
        if base is not None:
            if unchanged:
                rows = base
            else:
                # Same protocol the strict reader trusts (``rr.read``): every replica write stamps a new seq, so
                # a commit elsewhere (sessions, gateway rows) is a zero-row delta, not a full re-read. A rewrite
                # the seq cannot see fails the probe totals and falls through to the full read below.
                rows = rr._delta(conn, base, probe)
        if rows is None:
            rows = rr._full(conn, str(path), fence[:2], 'version', probe)
        if _file_fence(path) != fence:
            raise ValueError('replica file changed during snapshot')
        self._conn, self._fence, self._version = conn, identity, version
        self._probe, self._seq_probe, self._rows = probe, seq_probe, rows
        return ReplicaSnapshot(rows, gateway, infos, last_ok, offline)
