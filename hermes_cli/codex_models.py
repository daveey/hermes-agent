"""Codex model discovery from API, local cache, and config."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import List, Optional

from hermes_cli.route_identity import normalize_route_base_url

logger = logging.getLogger(__name__)


# Curated offline fallback (first-run, transient API failure). Only slugs the ChatGPT Codex
# OAuth backend actually accepts: the public API's "-pro" variants and the retired
# gpt-5.3-codex / gpt-5.2-codex / gpt-5.1-codex-max / gpt-5.1-codex-mini return HTTP 400 there
# ("not supported when using Codex with a ChatGPT account"), so listing them leaked dead picker
# choices (#52492). If OpenAI re-enables any, live discovery (_fetch_models_from_api) picks them
# up automatically.
DEFAULT_CODEX_MODELS: list[str] = [
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4-mini",
    "gpt-5.4",
    # Research preview exposed ONLY via the Codex OAuth backend for ChatGPT Pro subscribers —
    # not in the public API, so it stays out of the "openai" catalog in hermes_cli/models.py.
    # The backend reports ``supported_in_api: false`` for it; that flag describes API
    # availability, not Codex availability, so fetch/cache paths must not filter on it.
    "gpt-5.3-codex-spark"]

# gpt-5.3-codex-spark is in research preview and is exposed *only* via the Codex CLI / OAuth backend
# (chatgpt.com/backend-api/codex/models) for ChatGPT Pro subscribers. It is NOT available in the public
# OpenAI API, so it intentionally stays out of the "openai" provider catalog in hermes_cli/models.py — only
# the openai-codex (OAuth) provider surfaces it. The Codex backend reports ``supported_in_api: false`` for
# this slug; that flag describes API availability, not Codex backend availability, so the fetch/cache code
# paths below intentionally do not filter on it. PR #12994 removed this entry on the assumption it was
# unsupported — that was wrong; restored here. Keep it in the curated fallback so Pro users still see Spark
# in `/model` when live discovery is unavailable (offline first run, transient API failure).
_FORWARD_COMPAT_TEMPLATE_MODELS: list[tuple[str, tuple[str, ...]]] = [
    ("gpt-6-sol", ("gpt-5.6-sol", "gpt-5.5")),
    ("gpt-6-luna", ("gpt-5.6-luna", "gpt-5.5")),
    ("gpt-5.6-sol", ("gpt-5.5", "gpt-5.4")),
    ("gpt-5.6-terra", ("gpt-5.5", "gpt-5.4")),
    ("gpt-5.6-luna", ("gpt-5.5", "gpt-5.4")),
    ("gpt-5.5", ("gpt-5.4", "gpt-5.4-mini")),
    # Spark surfaces whenever a compatible template is present; the backend (not Hermes)
    # gates real availability by ChatGPT Pro entitlement.
    ("gpt-5.3-codex-spark", ("gpt-5.4", "gpt-5.5"))]


def _dedupe(model_ids) -> list[str]:
    """Order-preserving dedupe."""
    return list(dict.fromkeys(model_ids))


def _add_forward_compat_models(model_ids: list[str]) -> list[str]:
    """Surface newer Codex slugs missing from live discovery when an older compatible template is
    present (Clawdbot-style synthetic forward-compat catalog)."""
    ordered = _dedupe(model_ids)
    seen = set(ordered)
    for synthetic_model, template_models in _FORWARD_COMPAT_TEMPLATE_MODELS:
        if synthetic_model not in seen and any(template in seen for template in template_models):
            ordered.append(synthetic_model)
            seen.add(synthetic_model)
    return ordered


def _add_context_variants(model_ids: list[str]) -> list[str]:
    """Insert ``<slug>-900k`` large-context picker variants after eligible base slugs.

    Base slugs keep the cheaper advertised 272K limit; the variant opts into the large window.
    The suffix is Hermes-side only — stripped before the id hits the wire (agent/transports/codex.py,
    agent/auxiliary_client.py).
    """
    from agent.model_metadata import CODEX_CONTEXT_VARIANT_SUFFIX, has_codex_context_variant

    out: list[str] = []
    present = set(model_ids)
    for model_id in model_ids:
        out.append(model_id)
        variant = model_id + CODEX_CONTEXT_VARIANT_SUFFIX
        if variant in present or variant in out:
            continue
        if has_codex_context_variant(model_id):
            out.append(variant)
    return out


def _finalize_codex_models(model_ids: list[str]) -> list[str]:
    """Forward-compat synthesis + large-context variant synthesis."""
    return _add_context_variants(_add_forward_compat_models(model_ids))


def _drop_undiscovered_astra(model_ids: list[str]) -> list[str]:
    """Astra is account-gated: only the live account-scoped catalog may advertise it. A stale
    ``models_cache.json`` or a ``config.toml`` default is a compatibility hint, not entitlement."""
    from agent.reasoning_effort import is_astra_model

    return [model for model in model_ids if not is_astra_model(model)]


def codex_catalog_credentials() -> list[tuple[str, Optional[str]]]:
    """``[(access_token, base_url)]`` for every usable Codex credential, strongest plan first.

    Discovery must not depend on which pool entry round-robin happens to point at: a free-plan
    account lists no Astra, so a catalog built from it hides models the pro account in the same pool
    can run (and the picker then offers them only under the metered OpenAI API). Read-only: nothing
    here refreshes, imports or persists a credential; expired tokens are skipped."""
    from agent.credential_pool import STATUS_DEAD
    from agent.codex_entitlements import codex_plan_type
    from hermes_cli.auth import _codex_access_token_is_expiring, read_credential_pool, resolve_codex_runtime_credentials
    from hermes_cli.auth_codex import _codex_pool_dicts, _codex_pool_route_base_url

    from agent.credential_pool_disabled import token_disabled

    found: list[tuple[str, Optional[str]]] = []
    seen: set[str] = set()

    def _add(token: object, base_url: object) -> None:
        tok = str(token or "").strip()
        if not tok or tok in seen or _codex_access_token_is_expiring(tok, 0):
            return
        if token_disabled("openai-codex", tok):  # `hermes auth disable`: never sent, not even for a catalog
            return
        seen.add(tok)
        found.append((tok, (str(base_url).strip() if base_url else "") or _codex_pool_route_base_url("") or None))

    try:
        creds = resolve_codex_runtime_credentials(read_only=True)
        _add(creds.get("api_key"), creds.get("base_url"))
    except Exception:  # no/exhausted singleton creds: the pool below may still hold usable rows
        logger.debug("Codex singleton discovery unavailable", exc_info=True)
    try:
        for entry in _codex_pool_dicts(read_credential_pool("openai-codex")):
            if entry.get("last_status") == STATUS_DEAD:
                continue
            _add(entry.get("access_token"), _codex_pool_route_base_url(entry.get("base_url")))
    except Exception:
        logger.debug("Codex pool catalog credential scan failed", exc_info=True)

    _plan_rank = {"pro": 0, "prolite": 1, "team": 2, "business": 2, "enterprise": 2, "plus": 3}
    found.sort(key=lambda item: _plan_rank.get(codex_plan_type(item[0]) or "", 4))  # stable
    return found


def codex_catalog_credential_identity() -> str:
    """Identity of the credential SET live discovery would use right now, for the catalog cache key.

    Access/refresh tokens rotate in place while the account-scoped catalog stays authoritative for
    the same ChatGPT principal and route, so the key includes the principals and resolved base URLs,
    not the rotating tokens. The key covers every usable pool entry (not the round-robin cursor), so
    moving the cursor never changes it while adding/removing/upgrading an account does. An expired
    token is its own state (per route): ``_codex_catalog`` serves the static fallback for it, and that
    fallback must not outlive the refresh under the healthy principal's key. Opaque non-JWT tokens
    fall back to the token itself (the caller hashes every part before anything is persisted).
    """
    from agent.credential_pool import _codex_principal_identity
    from agent.codex_entitlements import codex_plan_type

    try:
        creds = codex_catalog_credentials()
    except Exception:
        logger.debug("Codex catalog identity unavailable", exc_info=True)
        creds = []
    if not creds:
        return "missing"
    parts = []
    for token, base_url in creds:
        principal = _codex_principal_identity(token)
        ident = ("/".join(principal) if principal else token) + "#" + (codex_plan_type(token) or "")
        parts.append(ident + "\n" + normalize_route_base_url(str(base_url or "").strip()))
    return "\n".join(sorted(parts))


def _ranked_slugs(entries: object) -> list[str]:
    """Visible slugs from a Codex catalog ``models`` list, sorted by (priority, slug), deduped.

    Does not filter on ``supported_in_api``: that flag describes the public OpenAI API, while the
    OAuth-backed Codex backend still accepts slugs marked false there (gpt-5.3-codex-spark).
    """
    sortable = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        visibility = item.get("visibility")
        if isinstance(visibility, str) and visibility.strip().lower() in {"hide", "hidden"}:
            continue
        priority = item.get("priority")
        rank = int(priority) if isinstance(priority, (int, float)) else 10_000
        sortable.append((rank, slug.strip()))

    sortable.sort()
    return _dedupe(slug for _, slug in sortable)


def _fetch_models_from_api(access_token: str, base_url: Optional[str] = None) -> list[str]:
    """Fetch available models from the Codex API. Returns visible models sorted by priority.

    ``base_url`` is the host the credential is routed to (resolved together with it); the
    catalog is fetched there, never from a host the credential does not belong to (#121486).
    """
    try:
        from agent.model_metadata import _codex_catalog_probe_allowed
        from hermes_cli.auth_codex import _codex_base_url
        catalog_base = (base_url or "").strip().rstrip("/") or _codex_base_url()
        if not _codex_catalog_probe_allowed(access_token, catalog_base):
            return []
        import httpx
        # The per-account catalog needs ChatGPT-Account-ID (else ``{"models":[]}`` with HTTP 200
        # masquerades as "no models") and, for residency-enforced workspaces, the residency header.
        from agent.codex_headers import codex_account_headers
        headers = {"Authorization": f"Bearer {access_token}", **codex_account_headers(access_token)}
        from agent.model_metadata import fetch_codex_catalog_entries
        entries, _status = fetch_codex_catalog_entries(
            lambda url: httpx.get(url, headers=headers, timeout=10), base_url=catalog_base)
    except Exception as exc:
        logger.debug("Failed to fetch Codex models from API: %s", exc)
        return []

    return _finalize_codex_models(_ranked_slugs(entries))


def _read_default_model(codex_home: Path) -> Optional[str]:
    config_path = codex_home / "config.toml"
    if not config_path.exists():
        return None
    try:
        import tomllib
        import tomllib
        payload = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    model = payload.get("model") if isinstance(payload, dict) else None
    return model.strip() if isinstance(model, str) and model.strip() else None


def _read_cache_models(codex_home: Path) -> list[str]:
    cache_path = codex_home / "models_cache.json"
    if not cache_path.exists():
        return []
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []

    entries = raw.get("models") if isinstance(raw, dict) else None
    return _ranked_slugs(entries if isinstance(entries, list) else [])


def get_codex_model_ids_for_credentials(credentials: list[tuple[str, Optional[str]]]) -> list[str]:
    """Union of the live catalogs of every credential (strongest plan first), so an entitled account's
    models are listed whichever account is current. With no live answer from any, falls back to the
    offline hints exactly like :func:`get_codex_model_ids`."""
    merged: list[str] = []
    for token, base_url in credentials:
        api_models = _fetch_models_from_api(token, base_url=base_url)
        if api_models:
            merged.extend(m for m in _finalize_codex_models(api_models) if m not in merged)
    if merged:
        return merged
    return get_codex_model_ids()


def get_codex_model_ids(access_token: Optional[str] = None, base_url: Optional[str] = None) -> list[str]:
    """Available Codex model IDs: live API (if token) > config.toml default > local cache > defaults.

    Pass the ``base_url`` resolved together with ``access_token`` (runtime/pool route) so live
    discovery asks the credential's own host. Without a live answer the result is
    ``CuratedFallbackModels``: a cache placeholder that never replaces a verified catalog, since
    account-gated rows such as Astra are absent from the offline hints."""
    codex_home = Path(os.getenv("CODEX_HOME", "").strip() or str(Path.home() / ".codex")).expanduser()
    if access_token:
        api_models = _fetch_models_from_api(access_token, base_url=base_url)
        if api_models:
            return _finalize_codex_models(api_models)
    # Late: models_catalog_static builds its codex table from this module at import time.
    from hermes_cli.models_catalog_static import CuratedFallbackModels

    default_model = _read_default_model(codex_home)
    return CuratedFallbackModels(_finalize_codex_models(_drop_undiscovered_astra(_dedupe([
        *([default_model] if default_model else []), *_read_cache_models(codex_home),
        *DEFAULT_CODEX_MODELS]))))
