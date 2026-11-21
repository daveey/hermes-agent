"""Overlap cold Codex catalog I/O with independent boot work, without stale metadata.

Only in-flight requests are shared. The existing metadata function owns authentication,
route checks, TTLs, negative results, caps and service-tier publication.
"""
from concurrent.futures import Future
import logging
import threading
from agent.memory_provider import spawn_context_thread

logger = logging.getLogger(__name__)


def fetch(access_token: str, base_url: str = "") -> tuple[dict[str, int], bool]:
    """Existing account-scoped fetch; all cache/security seams remain on model_metadata."""
    from agent import model_metadata as mm
    if not mm._codex_catalog_probe_allowed(access_token, base_url):
        return {}, False
    now = mm.time.time()
    cache_key = mm._codex_oauth_token_fingerprint(access_token, base_url)
    cached = mm._codex_oauth_context_cache.get(cache_key)
    if cached is not None and now - cached[1] < mm._CODEX_OAUTH_CONTEXT_CACHE_TTL:
        return cached[0], False
    # Workspace account/residency headers must accompany the selected credential.
    from agent.codex_headers import codex_account_headers
    headers = {"Authorization": f"Bearer {access_token}", **codex_account_headers(access_token)}
    try:
        entries, status = mm.fetch_codex_catalog_entries(
            lambda url: mm.model_metadata_http.get(url, headers=headers, timeout=(5, 10), verify=mm.model_metadata_http.resolve_verify()),
            base_url=base_url,
        )
        if status != 200:
            logger.debug("Codex /models probe returned HTTP %s; falling back to hardcoded defaults", status)
            mm._remember_no_codex_catalog(cache_key)
            return {}, False
    except Exception:
        logger.debug("Codex /models probe failed", exc_info=True)
        mm._remember_no_codex_catalog(cache_key)
        return {}, False
    from agent.codex_service_tiers import remember_catalog
    remember_catalog(cache_key, entries)
    result: dict[str, int] = {}
    max_result: dict[str, int] = {}
    for item in entries:
        slug, ctx, max_ctx = (item.get("slug"), item.get("context_window"), item.get("max_context_window")) if isinstance(item, dict) else (None, None, None)
        if isinstance(slug, str) and isinstance(ctx, int) and ctx > 0:
            result[slug.strip()] = ctx
            if isinstance(max_ctx, int) and max_ctx > 0:
                max_result[slug.strip()] = max_ctx
    if not result:
        mm._remember_no_codex_catalog(cache_key)
        return {}, False
    # Max first: a reader that sees the fresh context entry must also see its cap.
    with mm.MEMO_LOCK:
        mm.bound_codex_caches(cache_key)
        mm._codex_oauth_max_context_cache[cache_key] = max_result
        mm._codex_oauth_context_cache[cache_key] = (result, now)
    return result, True

_lock = threading.Lock()
_inflight: dict[str, Future] = {}


def resolve(token, base_url, fetch):
    """Join the same token/endpoint request or perform it with the existing fetcher."""
    from agent.model_metadata import _codex_oauth_token_fingerprint
    key = _codex_oauth_token_fingerprint(token, base_url)
    with _lock:
        pending = _inflight.get(key)
        owner = pending is None
        if owner:
            pending = _inflight[key] = Future()
    if not owner:
        return pending.result()[0], False
    try:
        result = fetch(token, base_url)
        pending.set_result(result)
        return result
    except BaseException as exc:
        pending.set_exception(exc)
        raise
    finally:
        with _lock:
            if _inflight.get(key) is pending:
                del _inflight[key]


def prefetch(token, base_url):
    """Start one daemon request with this caller's profile, TLS and secret scope."""
    result = Future()

    def run():
        from agent.model_metadata import _fetch_codex_oauth_context_lengths_with_source
        try:
            result.set_result(_fetch_codex_oauth_context_lengths_with_source(token, base_url))
        except BaseException as exc:
            logger.debug('catalog prefetch failed', exc_info=True)
            result.set_exception(exc)

    spawn_context_thread(run, name='codex-catalog-prefetch').start()
    return result


def prefetch_agent(agent):
    """Only after credential selection; pinned/ambiguous routes keep their old lazy path."""
    if agent.provider != 'openai-codex' or not agent.api_key:
        return
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
        model = config.get('model') or {}
        if not isinstance(model, dict) or model.get('context_length') is not None or config.get('custom_providers'):
            return
    except Exception:
        logger.debug('catalog prefetch configuration unavailable', exc_info=True)
        return  # This is optional overlap, never a new startup failure.
    try:
        return prefetch(agent.api_key, agent.base_url)
    except (RuntimeError, OSError) as exc:
        from hermes_cli.worker_drain_fence import DrainRefused
        if isinstance(exc, DrainRefused):
            raise  # Optional overlap must not bypass physical-custody admission.
        logger.debug('catalog prefetch could not start; using synchronous resolution', exc_info=True)
        return None
