"""User-disabled pool credentials: rows the user switched off without removing them.

The list lives in its own auth.json section, ``disabled_credentials: {provider: [{"id", "label"}]}``,
never on the pool rows. Every pool writer (rotation, exhaustion, refresh, the stale-writer merge, hub
pulls) rewrites rows from its in-memory copy, so a flag stored on a row would be undone by the next
flush from any process that loaded the pool before the user disabled it. Only
:func:`set_credential_disabled` writes this section, under the auth-store lock.

A record matches a row by id OR by case-insensitive label. Label matching keeps an account disabled
when its row is recreated with a new id (a re-login, a hub pull, a singleton re-seed), which all keep
the account's label (its email).

Readers union the active store's section with the global root's, so a disable in the root home
applies to every profile on the host. Reads are cached by each file's (mtime, size), so a
``hermes auth disable`` run from another process reaches a live pool on its next selection.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

SECTION = "disabled_credentials"

_cache_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[tuple[int, int], tuple[frozenset[str], frozenset[str]]]] = {}
_EMPTY: tuple[frozenset[str], frozenset[str]] = (frozenset(), frozenset())


def _norm_label(label: Any) -> str:
    return str(label or "").strip().lower()


def _records(store: dict[str, Any], provider: str) -> list[dict[str, Any]]:
    section = store.get(SECTION)
    rows = section.get(provider) if isinstance(section, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _keys_from_records(records: Iterable[dict[str, Any]]) -> tuple[frozenset[str], frozenset[str]]:
    ids = frozenset(str(r["id"]) for r in records if r.get("id"))
    labels = frozenset(_norm_label(r.get("label")) for r in records if _norm_label(r.get("label")))
    return ids, labels


def _file_keys(path: Optional[Path], provider: str) -> tuple[frozenset[str], frozenset[str]]:
    if path is None:
        return _EMPTY
    try:
        st = os.stat(path)
    except OSError:
        return _EMPTY
    stamp = (st.st_mtime_ns, st.st_size)
    key = (str(path), provider)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    try:
        # Plain read, not hermes_cli.auth._load_auth_store: this runs on request paths, and that loader
        # has side effects (corrupt-copy, migration). Unreadable or mid-write: keep the last good answer.
        keys = _keys_from_records(_records(json.loads(path.read_text(encoding="utf-8-sig")), provider))
    except (OSError, ValueError):
        return hit[1] if hit is not None else _EMPTY
    with _cache_lock:
        _cache[key] = (stamp, keys)
    return keys


def _root_path() -> Optional[Path]:
    """Global-root auth.json in profile mode (None in classic mode, or the real home under pytest)."""
    from agent.credential_pool import _guarded_global_root
    from hermes_cli import auth as auth_mod
    root = _guarded_global_root(auth_mod._global_auth_file_path())
    return Path(root) if root is not None else None


def disabled_keys(provider: str) -> tuple[frozenset[str], frozenset[str]]:
    """``(ids, lowercase labels)`` the user disabled for *provider* (active store + global root)."""
    from hermes_cli import auth as auth_mod
    active = auth_mod._auth_file_path()
    root = _root_path()
    ids, labels = _file_keys(active, provider)
    if root is not None and root != Path(active):
        root_ids, root_labels = _file_keys(root, provider)
        ids, labels = ids | root_ids, labels | root_labels
    return ids, labels


def matches(entry: Any, keys: tuple[frozenset[str], frozenset[str]]) -> bool:
    ids, labels = keys
    if not ids and not labels:
        return False
    return getattr(entry, "id", None) in ids or (
        bool(labels) and _norm_label(getattr(entry, "label", "")) in labels
    )


def enabled_entries(provider: str, entries: Iterable[Any]) -> list[Any]:
    """*entries* minus the ones the user disabled for *provider* (one section read per call)."""
    keys = disabled_keys(provider)
    return [e for e in entries if not matches(e, keys)]


def is_credential_disabled(provider: str, entry: Any) -> bool:
    return matches(entry, disabled_keys(provider))


def set_credential_disabled(provider: str, entry: Any, disabled: bool) -> bool:
    """Disable or re-enable *entry* in the ACTIVE store. Returns True when the section changed.

    Enabling drops every record matching the entry's id or label. A record that lives only in the
    global root is not touched from a profile; :func:`disabled_by_root` tells the caller so.
    """
    from hermes_cli import auth as auth_mod
    entry_id = str(getattr(entry, "id", "") or "")
    label = str(getattr(entry, "label", "") or "")
    with auth_mod._auth_store_lock():
        store = auth_mod._load_auth_store()
        section = store.get(SECTION)
        if not isinstance(section, dict):
            section = {}
        records = _records(store, provider)
        keys = _keys_from_records(records)
        hit = matches(entry, keys)
        if disabled:
            if hit:
                return False
            records.append({"id": entry_id, "label": label})
        else:
            if not hit:
                return False
            records = [r for r in records if not matches(entry, _keys_from_records([r]))]
        if records:
            section[provider] = records
        else:
            section.pop(provider, None)
        if section:
            store[SECTION] = section
        else:
            store.pop(SECTION, None)
        auth_mod._save_auth_store(store)
    return True


def disabled_by_root(provider: str, entry: Any) -> bool:
    """True when the global root (not the active profile) holds a record disabling *entry*."""
    from hermes_cli import auth as auth_mod
    root = _root_path()
    if root is None or root == Path(auth_mod._auth_file_path()):
        return False
    return matches(entry, _file_keys(root, provider))
