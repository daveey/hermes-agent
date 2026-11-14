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


def config():
    """The pass's config snapshot: alias normalization runs per run row, and a
    cache-hit config load (stat + signature) per row dominated the lanes.

    Outside a scope this is a fresh read. A failed load is never retained. Any
    input read whose live aliases differ from the snapshot refreshes it (``_key``).
    """
    from hermes_cli.config import load_config_readonly

    scope = _SCOPE.get()
    if scope is None:
        return load_config_readonly()
    slot = ('config', get_ident())
    held = scope.get(slot)
    if held is None:
        held = load_config_readonly()
        _hold_config(scope, held)
    return held


def _hold_config(scope, cfg):
    # The alias copy, not the dict identity, is compared later: an in-place edit of
    # a shared config dict must still drop memos derived from the old aliases.
    scope[('config', get_ident())] = cfg
    scope[('aliases', get_ident())] = deepcopy(_aliases(cfg))
    _drop_memos(scope)


def memo(name, key, compute):
    """Scope-local memo for pure functions of their key and the pass's config
    snapshot (``config``); replacing that snapshot drops every memo. Values
    must be immutable. Outside a scope this just computes."""
    scope = _SCOPE.get()
    if scope is None:
        return compute()
    table = scope.setdefault(('memo', get_ident(), name), {})
    try:
        return table[key]
    except KeyError:
        value = table[key] = compute()
        return value


def _drop_memos(scope):
    for slot in [s for s in scope if s[0] == 'memo' and s[1] == get_ident()]:
        del scope[slot]


def _aliases(cfg):
    # A non-mapping ``kanban`` section is a config error the dispatcher already
    # fails closed on (dispatch allowlist); the identity read must not crash on it.
    section = cfg.get('kanban') if isinstance(cfg, dict) else None
    return section.get('shared_blocker_aliases', {}) if isinstance(section, dict) else {}


def _snapshot_aliases(scope):
    """The aliases the held snapshot carries now (its live content, so an in-place
    edit counts), or None when no snapshot is held."""
    held = scope.get(('config', get_ident()))
    return None if held is None else _aliases(held)


def _key(conn, now, aliases=None):
    """Commit/config identity. ``aliases`` given: the caller vouches for them and
    the live config is not re-read (``read``'s post-compute check)."""
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
        if aliases is None:
            live = load_config_readonly()
            aliases = _aliases(live)
            scope = _SCOPE.get() or {}
            held = scope.get(('aliases', get_ident()))
            if ('config', get_ident()) in scope and held != aliases:
                _hold_config(scope, live)
        return (conn, conn.total_changes, version, schema, tuple(files), now, deepcopy(aliases))
    except (sqlite3.Error, OSError, ConfigBackendUnavailable):
        return None


def read(name, conn, now, compute):
    """Bracket inputs with their commit/config identity; unknown means uncached.

    Transactions (including savepoints) always bypass. total_changes catches
    same-connection writes even after rollback; data_version catches peers.
    Scope exit drops all retained handles and observations.

    Only the pre-compute key reads the live config: ``compute`` sees config
    solely through the pass snapshot (``config``), so the value is fresh for
    the key's aliases iff the snapshot still carries them afterwards. A live
    edit during ``compute`` is caught by the next read's key. With no snapshot
    held the post-compute key falls back to a live read.
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
    if _key(conn, now, _snapshot_aliases(scope)) == key:
        scope[slot] = (key, deepcopy(value))
    return value
