"""User-disabled pool credentials: rows the user switched off without removing them.

The list lives in its own auth.json section, ``disabled_credentials: {provider: [{"id", "label"}]}``,
never on the pool rows. Every pool writer (rotation, exhaustion, refresh, the stale-writer merge, hub
pulls) rewrites rows from its in-memory copy, so a flag stored on a row would be undone by the next
flush from any process that loaded the pool before the user disabled it. Only
:func:`set_credential_disabled` writes this section, under the auth-store lock.

A record ``{"id", "label", "account"}`` matches a row by id OR by case-insensitive label, so an account
stays disabled when its row is recreated with a new id (a re-login, a hub pull). Generic labels that
name a slot, not an account (``device_code``, ``claude_code``, ``env:...``), never match by label.
``account`` is the identity in the row's JWT (email or ChatGPT account id) when it has one: a row
whose token names a DIFFERENT account never matches, so disabling the Codex/xAI singleton slot does
not also disable whoever signs into that slot next.

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
_EMPTY: tuple = (frozenset(), frozenset(), frozenset())
_GENERIC_LABELS = frozenset({"device_code", "claude_code", "hermes_pkce", "manual", "oauth", "api_key", "env"})
# Only the providers whose every token path checks this list (pool, singleton, env/borrowed tokens).
SUPPORTED_PROVIDERS = frozenset({"anthropic", "openai-codex", "xai-oauth"})


def _norm_label(label: Any) -> str:
    return str(label or "").strip().lower()


def _records(store: dict[str, Any], provider: str) -> list[dict[str, Any]]:
    section = store.get(SECTION)
    rows = section.get(provider) if isinstance(section, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _generic(label: str) -> bool:
    return label in _GENERIC_LABELS or label.startswith(("env:", "env ")) or not label


def account_of(entry: Any) -> Optional[str]:
    """Lowercase account identity from the row's JWT access token (email, else ChatGPT account id)."""
    from hermes_cli.auth_constants import _decode_jwt_claims
    claims = _decode_jwt_claims(getattr(entry, "access_token", None) or "")
    if not isinstance(claims, dict) or not claims:
        return None
    profile = claims.get("https://api.openai.com/profile") or {}
    auth = claims.get("https://api.openai.com/auth") or {}
    for value in (claims.get("email"), profile.get("email") if isinstance(profile, dict) else None,
                  auth.get("chatgpt_account_id") if isinstance(auth, dict) else None):
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def _keys_from_records(records: Iterable[dict[str, Any]]) -> tuple:
    """``(ids, labels, guards)``; guards = ``(id, account)`` pairs a matching row's token must not contradict."""
    records = list(records)
    ids = frozenset(str(r["id"]) for r in records if r.get("id"))
    labels = frozenset(lab for r in records for lab in (_norm_label(r.get("label")), _norm_label(r.get("account")))
                       if not _generic(lab))
    guards = frozenset((str(r["id"]), _norm_label(r["account"])) for r in records if r.get("id") and r.get("account"))
    return ids, labels, guards


def _file_keys(path: Optional[Path], provider: str) -> tuple:
    if path is None:
        return _EMPTY
    try:
        st = os.stat(path)
    except OSError:
        return _EMPTY
    stamp = (st.st_mtime_ns, st.st_size, st.st_ino)  # ino: the atomic-rename save always changes it
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


def disabled_keys(provider: str) -> tuple:
    """``(ids, lowercase labels, guards)`` the user disabled for *provider* (active store + global root)."""
    from hermes_cli import auth as auth_mod
    try:
        active, root = auth_mod._auth_file_path(), _root_path()
    except Exception:  # no resolvable store (or a guarded one) holds no disable records
        logger.debug("disabled-credential check: auth store path unavailable", exc_info=True)
        return _EMPTY
    keys = _file_keys(active, provider)
    if root is not None and root != Path(active):
        keys = tuple(a | b for a, b in zip(keys, _file_keys(root, provider)))
    return keys


def matches(entry: Any, keys: tuple) -> bool:
    ids, labels = keys[0], keys[1]
    if not ids and not labels:
        return False
    acct = account_of(entry)
    if acct is not None and acct in labels:
        return True
    entry_id = getattr(entry, "id", None)
    if entry_id in ids:
        wanted = {a for i, a in (keys[2] if len(keys) > 2 else ()) if i == entry_id}
        if not wanted or acct is None or acct in wanted:
            return True
    label = _norm_label(getattr(entry, "label", ""))
    return not _generic(label) and label in labels


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
            record = {"id": entry_id, "label": label}
            if (acct := account_of(entry)) is not None:
                record["account"] = acct
            records.append(record)
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


def token_disabled(provider: str, *tokens: Optional[str]) -> bool:
    """Whether any of *tokens* (access or refresh) belongs to a disabled account of *provider*.

    For the resolvers that hand out a token without selecting it from the pool (the auth.json
    singleton, env tokens, the Claude Code file). A JWT naming a disabled account is disabled outright;
    otherwise the token must be a disabled pool row's access/refresh token. Reads the persisted rows,
    never ``load_pool`` (seeding may call the very resolver asking). Free while nothing is disabled.
    """
    wanted = {t.strip() for t in tokens if isinstance(t, str) and t.strip()}
    if not wanted:
        return False
    keys = disabled_keys(provider)
    if not keys[0] and not keys[1]:
        return False
    from types import SimpleNamespace
    if any(account_of(SimpleNamespace(access_token=t)) in keys[1] for t in wanted):
        return True
    from hermes_cli import auth as auth_mod
    try:
        rows = auth_mod.read_credential_pool(provider)
    except Exception:
        logger.warning("disabled-credential check: %s pool unreadable; token allowed", provider, exc_info=True)
        return False
    rows = [SimpleNamespace(**{k: r.get(k) for k in ("id", "label", "access_token", "refresh_token", "agent_key")})
            for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)]
    return any(
        matches(r, keys) and wanted & {r.access_token, r.refresh_token, r.agent_key}
        for r in rows
    )


def rebind_off_disabled(agent: Any) -> None:
    """Pre-request: move a Codex/xAI session whose bound pool row was disabled onto an enabled row.

    Anthropic sessions get the same through the pinned-token check (``credential_pool_pin``).
    Raises ``AuthError`` when every row of the pool is disabled, so no request goes out on the old token.
    """
    pool = getattr(agent, "_credential_pool", None)
    provider = getattr(pool, "provider", None)
    if pool is None or provider not in ("openai-codex", "xai-oauth") or getattr(agent, "provider", None) != provider:
        return
    keys = disabled_keys(provider)
    if not keys[0] and not keys[1]:
        return
    entry_id, api_key = getattr(agent, "_credential_pool_entry_id", None), str(getattr(agent, "api_key", "") or "")
    entries = pool.entries()
    bound = next((e for e in entries if entry_id and e.id == entry_id), None) or next(
        (e for e in entries if api_key and getattr(e, "runtime_api_key", None) == api_key), None)
    if bound is None or not matches(bound, keys):
        return
    nxt = pool.select()
    token = (getattr(nxt, "runtime_api_key", None) or "").strip() if nxt is not None else ""
    if not token:
        _raise_all_disabled(provider)
    logger.warning("%s pool entry %s was disabled; moving the session to %s", provider, bound.id, nxt.id)
    agent._adopt_openai_credentials(token, str(agent.base_url or ""), reason="credential_disabled")
    agent._credential_pool_entry_id = nxt.id


def _raise_all_disabled(provider: str) -> None:
    from hermes_cli.auth import AuthError
    raise AuthError(f"Every {provider} account is disabled; run `hermes auth enable {provider} <account>`",
                    provider=provider, code="credentials_disabled")


def refuse_disabled_token(provider: str, *tokens: Optional[str]) -> None:
    """Raise ``AuthError`` when *tokens* (access/refresh) are a disabled account's: never send or spend them."""
    if token_disabled(provider, *tokens):
        _raise_all_disabled(provider)
