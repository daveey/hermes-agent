"""Call-scoped, validated input reuse for shared-blocker derivation.

Never retain derived BlockerState or a transaction across a shell probe. Values
are privately copied; a caller cannot mutate a later consumer's observations.
"""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from pathlib import Path
import sqlite3
from threading import get_ident

_SCOPE: ContextVar[dict | None] = ContextVar('blocker_input_scope', default=None)


def input_pass(fn):
    """Reuse only inside a synchronous derivation, including nested consumers."""
    @wraps(fn)
    def scoped(*args, **kwargs):
        if _SCOPE.get() is not None:
            return fn(*args, **kwargs)
        token = _SCOPE.set({})
        try:
            return fn(*args, **kwargs)
        finally:
            _SCOPE.reset(token)
    return scoped


def _key(conn, now):
    if conn.in_transaction:
        return None
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config_backend import ConfigBackendUnavailable

    try:
        path = next((r[2] for r in conn.execute('PRAGMA database_list') if r[1] == 'main'), '')
        files = []
        if path:
            for name in (path, path + '-shm'):
                try:
                    st = Path(name).stat()
                    files.append((st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size))
                except FileNotFoundError:
                    files.append(None)
        version = conn.execute('PRAGMA data_version').fetchone()[0]
        schema = conn.execute('PRAGMA schema_version').fetchone()[0]
        aliases = (load_config_readonly().get('kanban') or {}).get('shared_blocker_aliases', {})
        return (conn, conn.total_changes, version, schema, tuple(files), now, deepcopy(aliases))
    except (sqlite3.Error, OSError, ConfigBackendUnavailable):
        return None


def read(name, conn, now, compute):
    """Bracket inputs with their commit/config identity; unknown means uncached.

    Transactions (including savepoints) always bypass. total_changes catches
    same-connection writes even after rollback; data_version catches peers.
    Scope exit drops all retained handles and observations.
    """
    scope = _SCOPE.get()
    if scope is None:
        return compute()
    key = _key(conn, now)
    if key is None:
        scope.clear()
        return compute()
    slot = (get_ident(), conn, name)
    held = scope.get(slot)
    if held is not None and held[0] == key:
        return deepcopy(held[1])
    scope.pop(slot, None)
    value = compute()
    if _key(conn, now) == key:
        scope[slot] = (key, deepcopy(value))
    return value
