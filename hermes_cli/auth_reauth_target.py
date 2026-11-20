"""Atomic selected-row reauthentication primitives for owned OAuth grants.

Targets are backend-only, short-lived values. They are not serialized or accepted
from a renderer. Login runs between prepare and commit, outside the store lock.
Only tokens returned by the provider login exchange may reach commit_reauth.
"""
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any


class ReauthConflict(ValueError):
    """Recovery cannot safely replace the selected stored credential."""


@dataclass(frozen=True)
class ReauthTarget:
    provider: str
    credential_id: str
    store_path: Path = field(repr=False)
    generation: str = field(repr=False)
    principal: tuple[str, str] | None = field(repr=False)


@dataclass(frozen=True)
class ReauthResult:
    credential_id: str
    identity_verified: bool


_OWNED_SOURCES = {
    'anthropic': {'manual:hermes_pkce'},
    'openai-codex': {'manual:device_code', 'manual:loopback_pkce'},
}
_TOKEN_FIELDS = ('access_token', 'refresh_token', 'expires_at', 'expires_at_ms', 'last_refresh')


def _generation(row: dict[str, Any]) -> str:
    # Include source and policy metadata, not just the pair. Any target edit wins
    # over an outstanding login. Unrelated rows may still change concurrently.
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _selected(store: dict[str, Any], provider: str, credential_id: str) -> dict[str, Any]:
    pool = store.get('credential_pool')
    rows = pool.get(provider) if isinstance(pool, dict) else None
    hits = [row for row in rows if isinstance(row, dict) and row.get('id') == credential_id] if isinstance(rows, list) else []
    if len(hits) != 1:
        raise ReauthConflict('Selected credential is missing or ambiguous; refresh the account list.')
    row = hits[0]
    if row.get('auth_type') != 'oauth' or row.get('source') not in _OWNED_SOURCES.get(provider, set()):
        raise ReauthConflict('This credential must be recovered at its original credential source.')
    from agent.credential_pool_disabled import is_credential_disabled
    if is_credential_disabled(provider, SimpleNamespace(**row)):
        raise ReauthConflict('Selected credential is disabled; recovery did not change it.')
    return row


def _principal(provider: str, token: Any) -> tuple[str, str] | None:
    if provider != 'openai-codex':
        return None
    from agent.credential_pool import _codex_principal_identity
    return _codex_principal_identity(token)


def prepare_reauth(provider: str, credential_id: str) -> ReauthTarget:
    """Bind an exact locally-owned persisted row; never seed or borrow a pool."""
    from hermes_cli import auth
    if provider not in _OWNED_SOURCES or not isinstance(credential_id, str) or not credential_id:
        raise ReauthConflict('Unsupported recovery target.')
    path = auth._auth_file_path().resolve()
    with auth._auth_store_lock(target_path=path):
        row = _selected(auth._load_auth_store(path), provider, credential_id)
        return ReauthTarget(provider, credential_id, path, _generation(row),
                            _principal(provider, row.get('access_token')))


def _validate_grant(target: ReauthTarget, grant: dict[str, Any], allow_unverified: bool) -> bool:
    if any(not isinstance(grant.get(key), str) or not grant[key].strip()
           for key in ('access_token', 'refresh_token')):
        raise ReauthConflict('Provider did not return a complete OAuth grant.')
    principal = _principal(target.provider, grant['access_token'])
    if target.provider == 'openai-codex':
        if target.principal is None or principal != target.principal:
            raise ReauthConflict('Sign-in does not match the selected account and workspace.')
        return True
    if not allow_unverified:
        raise ReauthConflict('Account identity is unavailable; explicit replacement confirmation is required.')
    return False


def commit_reauth(target: ReauthTarget, grant: dict[str, Any], *,
                  allow_unverified_identity: bool = False) -> ReauthResult:
    """CAS a provider-exchanged grant into its selected row, preserving metadata.

    Unknown Anthropic identity is *never* reported verified. The caller must obtain
    explicit replacement consent before opting into that case. This result proves
    persistence only, not account health; callers must perform health readback.
    """
    from hermes_cli import auth
    if auth._auth_file_path().resolve() != target.store_path:
        raise ReauthConflict('Credential profile changed during sign-in.')
    verified = _validate_grant(target, grant, allow_unverified_identity)
    with auth._auth_store_lock(target_path=target.store_path):
        store = auth._load_auth_store(target.store_path)
        row = _selected(store, target.provider, target.credential_id)
        if _generation(row) != target.generation:
            raise ReauthConflict('Selected credential changed during sign-in; no credentials were saved.')
        for key in _TOKEN_FIELDS:
            row.pop(key, None)
            if key in grant:
                row[key] = grant[key]
        # A new grant replaces terminal authentication failure, not quota evidence.
        if row.get('last_status') == 'dead':
            for key in ('last_status', 'last_error', 'failure_reason', 'last_error_code',
                        'last_error_reason', 'last_error_message', 'last_error_reset_at'):
                row.pop(key, None)
        _selected(store, target.provider, target.credential_id)  # validate incoming disable policy BEFORE save
        auth._save_auth_store(store, target_path=target.store_path)
        saved = _selected(auth._load_auth_store(target.store_path), target.provider, target.credential_id)
        if _generation(saved) != _generation(row):
            raise ReauthConflict('Credential save could not be verified.')
    return ReauthResult(target.credential_id, verified)
