"""Read-only credential-pool snapshots for one synchronous picker build.

Writers always use auth._load_auth_store directly. No parsed object leaves this
module: callers get independent provider slices, never a shared working copy.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from threading import current_thread
from typing import Any

from utils import file_cache_version


@dataclass
class _ReadScope:
    owner: object = field(default_factory=current_thread)
    active: bool = True
    version: object = None
    store: dict[str, Any] | None = None


_SCOPE: ContextVar[_ReadScope | None] = ContextVar("picker_auth_pool_reads", default=None)


def _current_scope():
    scope = _SCOPE.get()
    # A copied context must not extend the pass or share its mutable snapshot
    # with a background catalog-refresh thread.
    return scope if scope is not None and scope.active and scope.owner is current_thread() else None


@contextmanager
def auth_pool_read_scope():
    """Nested reads share a snapshot; independent builds never retain one."""
    if _current_scope() is not None:
        yield
        return
    scope = _ReadScope()
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        scope.active = False
        scope.store = None
        _SCOPE.reset(token)


def _read_store():
    from hermes_cli import auth

    scope = _current_scope()
    if scope is None:
        return auth._load_auth_store()
    path = auth._auth_file_path()
    try:
        version = (str(path), file_cache_version(path))
    except OSError:
        scope.store = None
        return auth._load_auth_store()
    if scope.store is not None and scope.version == version:
        return scope.store
    store = auth._load_auth_store(path)
    # Capture before reading: a concurrent replacement must not label an old
    # parse with a newer version. The next read checks the path again.
    scope.version = version
    scope.store = store if isinstance(store.get("credential_pool"), dict) else None
    return store


def read_credential_pool(provider_id=None):
    """Preserve per-provider profile shadowing; copy only the requested slice."""
    from hermes_cli import auth

    pool = _read_store().get("credential_pool")
    pool = pool if isinstance(pool, dict) else {}
    global_pool = auth._load_global_auth_store().get("credential_pool")
    global_pool = global_pool if isinstance(global_pool, dict) else {}
    if provider_id is None:
        merged = dict(pool)
        for key, rows in global_pool.items():
            existing = merged.get(key)
            if isinstance(rows, list) and rows and not (isinstance(existing, list) and existing):
                merged[key] = rows
        return deepcopy(merged)
    rows = pool.get(provider_id)
    if not (isinstance(rows, list) and rows):
        rows = global_pool.get(provider_id)
    return deepcopy(rows) if isinstance(rows, list) else []


def known_pool_pairs(provider_id, rows):
    """Token-generation bases for the deletion fence, limited to rows of the store written.

    Rows read through the global-root fallback were never in the active store, so a missing
    one there is an addition, not a peer deletion. Single-use providers write back to the
    root, so their root-read rows stay known."""
    from agent.credential_pool import SINGLE_USE_REFRESH_POOL_PROVIDERS, _profile_owns_pool_provider
    from hermes_cli import auth

    pairs = auth._token_pairs_by_id(rows)
    if provider_id in SINGLE_USE_REFRESH_POOL_PROVIDERS and not _profile_owns_pool_provider(provider_id):
        return pairs
    own = auth._load_auth_store().get("credential_pool")
    own_rows = own.get(provider_id) if isinstance(own, dict) else None
    own_ids = set(auth._entry_ids(own_rows)) if isinstance(own_rows, list) else set()
    return {rid: pair for rid, pair in pairs.items() if rid in own_ids}
