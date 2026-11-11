"""User-disabled pool credentials: rows the user switched off without removing them.

The list lives in its own auth.json section, ``disabled_credentials: {provider: [{"id", "label"}]}``,
never on the pool rows. Every pool writer (rotation, exhaustion, refresh, the stale-writer merge, hub
pulls) rewrites rows from its in-memory copy, so a flag stored on a row would be undone by the next
flush from any process that loaded the pool before the user disabled it. Only
:func:`set_credential_disabled` writes this section, under the auth-store lock.

A record ``{"id", "label", "account", "fp", "src"}`` matches a row by any of:
- ``id``: the row itself (survives token refreshes, which keep the id);
- ``label`` (case-insensitive), unless it names a slot or counter, not an account (``device_code``,
  ``claude_code``, ``api-key-3``, env-var names like ``anthropic_token``): keeps a re-added row disabled;
- ``account``: the identity in the row's JWT (Codex: ChatGPT workspace id + user subject, so Team seats
  and one email's two workspaces stay separate; else the email). A record naming an account never hits
  a row whose token names a different one, so disabling the Codex singleton slot does not disable
  whoever signs into that slot next;
- ``fp``: a non-reversible fingerprint of the token at disable time;
- ``src``: for the Claude Code login, the source itself, so the token it holds NOW is refused too (it
  refreshes its own token outside Hermes). This disables that login slot: another account signed into
  Claude Code later stays refused until ``hermes auth enable``.

Readers union the active store's section with the global root's, so a disable in the root home
applies to every profile on the host. Reads are cached by each file's (mtime, size), so a
``hermes auth disable`` run from another process reaches a live pool on its next selection.
"""
from __future__ import annotations

import json
import re
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


# Numbered keys and env-var names (``anthropic_api_key``, ``claude_code_oauth_token``). Not every a_b:
# users name accounts ``work_main`` or ``david_personal``.
_GENERIC_RE = re.compile(r"api-key-\d+|[a-z0-9_]*_(api_key|key|token|oauth_token|secret)")


def _generic(label: str) -> bool:
    """Labels naming a slot or a counter, not an account: reused by unrelated rows, so never matched."""
    return not label or label in _GENERIC_LABELS or label.startswith(("env:", "env ")) or bool(
        _GENERIC_RE.fullmatch(label))


def account_of(entry: Any) -> Optional[str]:
    """Lowercase account identity from the row's JWT access token: ``<chatgpt_account_id>/<sub or email>``
    (Team seats share the workspace id; one email can own two workspaces), else the email, else the
    workspace id."""
    from hermes_cli.auth_constants import _decode_jwt_claims
    claims = _decode_jwt_claims(getattr(entry, "access_token", None) or "")
    if not isinstance(claims, dict) or not claims:
        return None
    profile = claims.get("https://api.openai.com/profile") or {}
    auth = claims.get("https://api.openai.com/auth") or {}
    workspace = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    subject = claims.get("sub")
    email = claims.get("email") or (profile.get("email") if isinstance(profile, dict) else None)
    if isinstance(workspace, str) and workspace.strip():
        for who in (subject, email):  # a seat of the workspace
            if isinstance(who, str) and who.strip():
                return f"{workspace.strip()}/{who.strip()}".lower()
    for value in (email, workspace):
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def fingerprint_of(entry: Any) -> Optional[str]:
    """Fingerprint of the row's live token; else the saved ``secret_fingerprint`` (borrowed/env rows persist
    no secret). Live first: right after a rotation the saved value still names the old token."""
    from agent.credential_persistence import fingerprint_secret_value
    live = fingerprint_secret_value(getattr(entry, "access_token", None) or "")
    if live:
        return live
    extra = getattr(entry, "extra", None)
    fp = (extra.get("secret_fingerprint") if isinstance(extra, dict) else None) or getattr(
        entry, "secret_fingerprint", None)
    return fp if isinstance(fp, str) and fp else None


def _borrowed_source(entry: Any) -> str:
    source = str(getattr(entry, "source", "") or "")
    # Only the Claude Code login rotates its own token outside Hermes; an env var keeps its value, which
    # the fingerprint already covers, and matching env:VAR by source would refuse any key put there later.
    return source if source == "claude_code" else ""


def _source_tokens(provider: str, source: str) -> set[str]:
    """Tokens a borrowed source holds right now (refreshed outside Hermes, so not in any record)."""
    if source == "claude_code" and provider == "anthropic":
        try:
            from agent.anthropic_credentials import read_claude_code_credentials
            creds = read_claude_code_credentials() or {}
        except Exception:
            logger.debug("disabled-credential check: Claude Code login unreadable", exc_info=True)
            return set()
        return {str(creds[k]).strip() for k in ("accessToken", "refreshToken") if creds.get(k)}
    return set()


def _keys_from_records(records: Iterable[dict[str, Any]]) -> tuple:
    """``(ids, markers, records)``: ids and markers only say "something is disabled" cheaply; ``records``
    holds one ``(id, label, account, fingerprint, borrowed source)`` tuple per record, matched one record at a time."""
    recs = frozenset(
        (str(r.get("id") or ""), "" if _generic(_norm_label(r.get("label"))) else _norm_label(r.get("label")),
         _norm_label(r.get("account")), str(r.get("fp") or ""), str(r.get("src") or ""))
        for r in records)
    recs = frozenset(t for t in recs if any(t))
    ids = frozenset(t[0] for t in recs if t[0])
    markers = frozenset(v for t in recs for v in t[1:] if v)
    return ids, markers, recs


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
    """A record hits a row by id, non-generic label, JWT account or secret fingerprint; a record that
    names an account never hits a row whose token names a different one (a reused slot, a namesake label)."""
    if not keys[0] and not keys[1]:
        return False
    entry_id = str(getattr(entry, "id", "") or "")
    label = _norm_label(getattr(entry, "label", ""))
    label = "" if _generic(label) else label
    acct = account_of(entry) or ""
    fp = fingerprint_of(entry) or ""
    src = _borrowed_source(entry)
    # An env:VAR row keeps its id when the variable's value changes: there the record means that value.
    env_row = str(getattr(entry, "source", "") or "").startswith("env:")
    for rid, rlabel, racct, rfp, rsrc in keys[2]:
        if racct and acct and racct != acct and not acct.startswith(racct + "/"):  # older records: workspace only
            continue
        if env_row and rfp and fp and rfp != fp:
            continue
        if ((rid and rid == entry_id) or (rlabel and rlabel == label) or (racct and racct == acct)
                or (rfp and rfp == fp) or (rsrc and rsrc == src)):
            return True
    return False


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
            if (fp := fingerprint_of(entry)) is not None:
                record["fp"] = fp  # non-reversible
            if src := _borrowed_source(entry):
                record["src"] = src  # whatever token this source holds later is refused too
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


def forget_removed(provider: str, removed: Any, remaining: Iterable[Any]) -> bool:
    """After ``hermes auth remove``: drop the records that disabled *removed* and match no remaining row,
    so a later row reusing its id or label is not born disabled. Active store only."""
    from hermes_cli import auth as auth_mod
    remaining = list(remaining)
    with auth_mod._auth_store_lock():
        store = auth_mod._load_auth_store()
        records = _records(store, provider)
        keep = [r for r in records if not matches(removed, _keys_from_records([r]))
                or any(matches(e, _keys_from_records([r])) for e in remaining)]
        if len(keep) == len(records):
            return False
        section = store.get(SECTION) if isinstance(store.get(SECTION), dict) else {}
        if keep:
            section[provider] = keep
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
    singleton, env tokens, the Claude Code file). Hit when a disabled record carries the token's
    fingerprint or JWT account, or a disabled persisted row holds the token (or, for borrowed rows that
    persist no secret, its fingerprint). Reads persisted rows, never ``load_pool`` (seeding may call the
    very resolver asking). Free while nothing is disabled.
    """
    from types import SimpleNamespace
    from agent.credential_persistence import fingerprint_secret_value
    wanted = {t.strip() for t in tokens if isinstance(t, str) and t.strip()}
    if not wanted:
        return False
    keys = disabled_keys(provider)
    if not keys[0] and not keys[1]:
        return False
    fps = {fingerprint_secret_value(t) for t in wanted}
    if any(matches(SimpleNamespace(id="", label="", access_token=t, extra={}), keys) for t in wanted):
        return True  # fingerprint or JWT account of a disabled record
    if any(wanted & _source_tokens(provider, rsrc) for *_, rsrc in keys[2] if rsrc):
        return True  # the current token of a disabled Claude Code login
    from agent.credential_pool import PooledCredential
    from hermes_cli import auth as auth_mod
    try:
        rows = auth_mod.read_credential_pool(provider)
        rows = [PooledCredential.from_dict(provider, r) for r in (rows if isinstance(rows, list) else [])
                if isinstance(r, dict)]
    except Exception:
        logger.warning("disabled-credential check: %s pool unreadable; token allowed", provider, exc_info=True)
        return False
    return any(
        matches(r, keys) and (wanted & {r.access_token, r.refresh_token, getattr(r, "agent_key", None)}
                              or (fingerprint_of(r) in fps))
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
