"""Bounded completion proof for stale model quota benches; only the caller mutates state."""
from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from contextvars import copy_context

MAX_RESPONSE_BYTES = 1 << 20


def _client(timeout):
    import httpx
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


def _valid_message(raw):
    """A completed Anthropic message: not error-shaped, not truncated, not headers-only."""
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    return (isinstance(data, dict) and data.get('type') == 'message' and data.get('role') == 'assistant'
            and isinstance(data.get('content'), list) and bool(data.get('stop_reason')))


async def _read_anthropic(response):
    chunks, size = [], 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            return False
        chunks.append(chunk)
    return _valid_message(b''.join(chunks))


async def _read_codex(response):
    buffer, size = b'', 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        buffer += chunk
        # Bound total bytes and any single unterminated line.
        if size > MAX_RESPONSE_BYTES or len(buffer) > MAX_RESPONSE_BYTES:
            return False
        *lines, buffer = buffer.split(b'\n')
        for raw in lines:
            line = raw.decode('utf-8', 'replace').rstrip('\r')
            if not line.startswith('data:'):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') in ('response.failed', 'response.incomplete', 'error'):
                return False
            if event.get('type') == 'response.completed':
                final = event.get('response')
                return isinstance(final, dict) and final.get('status') == 'completed'
    return False


async def _attempt(provider, url, headers, body, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False

    async def run():
        async with _client(remaining) as client, client.stream('POST', url, headers=headers, json=body) as response:
            if response.status_code != 200:
                return False
            return await (_read_anthropic if provider == 'anthropic' else _read_codex)(response)

    # Cancellation unwinds the async-with blocks, which closes the transport/socket.
    return bool(await asyncio.wait_for(run(), remaining)) and time.monotonic() < deadline


def completion_succeeds(provider, model, observed, deadline):
    """One request, no SDK retry/refresh, and no response or credential diagnostics.

    The absolute deadline is enforced by asyncio cancellation (closes the connection), so a
    trickling or newline-less peer cannot keep a worker thread alive past it.
    """
    import httpx
    if deadline - time.monotonic() <= 0:
        return False
    headers = {'Authorization': f'Bearer {observed.runtime_api_key}'}
    if provider == 'anthropic':
        url = 'https://api.anthropic.com/v1/messages'
        headers.update({'anthropic-version': '2023-06-01',
                        'anthropic-beta': 'claude-code-20250219,oauth-2025-04-20',
                        'user-agent': 'claude-code/2.1.280 (external, cli)', 'x-app': 'cli'})
        body = {'model': model, 'max_tokens': 1,
                'system': [{'type': 'text', 'text': "You are Claude Code, Anthropic's official CLI for Claude."}],
                'messages': [{'role': 'user', 'content': 'Reply OK.'}]}
    elif provider == 'openai-codex':
        from agent.codex_headers import CODEX_AUX_BASE_URL, codex_cloudflare_headers
        headers.update(codex_cloudflare_headers(observed.runtime_api_key))
        headers['Accept'] = 'text/event-stream'
        url = CODEX_AUX_BASE_URL + '/responses'
        # Codex requires stream + store=false and does not accept max_output_tokens.
        body = {'model': model, 'instructions': 'Reply OK.', 'store': False, 'stream': True,
                'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Reply OK.'}]}],
                'reasoning': {'effort': 'low'}}
    else:
        return False
    try:
        return asyncio.run(_attempt(provider, url, headers, body, deadline))
    except (httpx.HTTPError, OSError, ValueError, TimeoutError):
        return False


def _eligible(entry, provider, model, now, disabled):
    from agent.credential_pool import STATUS_DEAD, STATUS_EXHAUSTED
    from agent.credential_pool_disabled import matches
    from agent.credential_pool_model_cooldowns import model_cooldown_until
    if (entry.auth_type != 'oauth' or not entry.runtime_api_key or entry.last_status == STATUS_DEAD
            or entry.last_error_code in (401, 402, 403) or matches(entry, disabled)
            or entry.last_error_reason in ('billing', 'billing_unverified')
            or entry.extra.get('failure_reason') in ('billing', 'billing_unverified')):
        return False
    if entry.last_status == STATUS_EXHAUSTED:
        return provider == 'openai-codex' and entry.last_error_code == 429
    return (model_cooldown_until(entry, model) or 0) > now


def recover(provider, model, *, seconds=None):
    """Probe every eligible bench concurrently, CAS completed proofs before the shared deadline.

    Futures hold detached observations, never the pool. Timed-out futures can finish but cannot
    clear anything. Usage endpoints are intentionally not prerequisites: unknown usage is common.
    """
    from copy import deepcopy
    from agent.credential_pool import load_pool
    from agent.credential_pool_disabled import disabled_keys
    from hermes_cli.kanban_quota_probe import PROBE_DEADLINE_SECONDS
    deadline = time.monotonic() + (PROBE_DEADLINE_SECONDS if seconds is None else seconds)
    if provider not in ('anthropic', 'openai-codex') or not model:
        return False
    pool = load_pool(provider, allowed_auth_types={'oauth'})
    disabled = disabled_keys(provider)
    entries = [deepcopy(e) for e in pool.entries() if _eligible(e, provider, model, time.time(), disabled)]
    if not entries or time.monotonic() >= deadline:
        return False
    executor = ThreadPoolExecutor(max_workers=len(entries), thread_name_prefix='quota-proof')
    cleared = False
    verified = []
    try:
        futures = {executor.submit(copy_context().run, completion_succeeds, provider, model, e, deadline): e
                   for e in entries}
        try:
            for future in as_completed(futures, timeout=max(0, deadline - time.monotonic())):
                if time.monotonic() >= deadline:
                    break
                if not future.exception() and future.result() and time.monotonic() < deadline:
                    observed = futures[future]
                    if pool.clear_verified_model_cooldown(observed, model):
                        cleared = True
                        verified.append({'id': getattr(observed, 'id', ''),
                                         'cooldown_until': (observed.model_cooldowns or {}).get(model)
                                         or getattr(observed, 'last_error_reset_at', None)})
        except TimeoutError:
            pass
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    if cleared:
        import logging
        from hermes_cli.kanban_quota_detector import record_contradiction
        logging.getLogger(__name__).info(
            'QuotaContradicted cleared provider=%s model=%s accounts=%s',
            provider, model, len(verified))
        record_contradiction(provider, model, verified)
    return cleared


def quota_exception(exc):
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, SubscriptionQuotaUnavailable):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def recover_exception(exc):
    """Fail closed while preserving the original typed startup refusal."""
    wall = quota_exception(exc)
    if wall is None:
        return False
    try:
        return recover(wall.provider, wall.model)
    except Exception:  # health: allow BLE001 -- probe failure must preserve the original startup refusal
        return False
