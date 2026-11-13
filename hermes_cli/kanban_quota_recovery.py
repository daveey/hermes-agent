"""Bounded completion proof for stale model quota benches; only the caller mutates state."""
from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from contextvars import copy_context

MAX_RESPONSE_BYTES = 1 << 20
# A refused/failed proof benches that (provider, model, account) proof for this long across
# every process of the home: during a real wall each Kanban start would otherwise spend one
# real completion per benched account.
FAILED_PROOF_COOLDOWN_SECONDS = 600

# libc DNS has no cancellation API. Keep it outside the interpreter/executor so
# cancelling a probe can kill AND reap the resolver, including during loop shutdown.
_DNS_SCRIPT = """
import json, socket, sys
args = json.load(sys.stdin)
try:
    result = socket.getaddrinfo(*args)
except OSError:
    sys.exit(1)
json.dump(result, sys.stdout)
"""


async def _getaddrinfo(host, port, *, family=0, type=0, proto=0, flags=0):
    import subprocess
    import sys
    import tempfile
    from tools.environments.local import build_subprocess_env

    if isinstance(host, bytes):
        host = host.decode('ascii')
    # A file avoids pipe backpressure and transport finalizers. Reaping below is
    # synchronous after SIGKILL, so nested AnyIO cancellation cannot interrupt it.
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(  # noqa: ASYNC220 -- synchronous spawn preserves kill/reap ownership on cancellation
            [sys.executable, '-I', '-S', '-c', _DNS_SCRIPT],
            stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
            env=build_subprocess_env(base={}))
        try:
            try:
                process.stdin.write(json.dumps([host, port, family, type, proto, flags]).encode('utf-8'))
                process.stdin.close()
            except BrokenPipeError:
                pass  # the child already exited; its exit status below decides the result
            while process.poll() is None:
                await asyncio.sleep(0.01)
            if process.returncode != 0:
                raise OSError('DNS resolution failed')
            output.seek(0)
            rows = json.loads(output.read(MAX_RESPONSE_BYTES))
            return [(af, kind, protocol, name, tuple(address))
                    for af, kind, protocol, name, address in rows]
        finally:
            process.stdin.close()
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                # Not reaped within 1 s (killed child stuck in the kernel, or never killed): kill
                # again and give it one more bounded reap; Popen's own cleanup list reaps a
                # survivor later. Never let TimeoutExpired replace the closed result.
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=1)


async def _run_attempt(provider, url, headers, body, deadline):
    # Loop-local replacement: no global socket patch and no uncancellable DNS
    # work in asyncio's default executor (which asyncio.run must join on exit).
    asyncio.get_running_loop().getaddrinfo = _getaddrinfo
    return await _attempt(provider, url, headers, body, deadline)


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

    Cancellation closes HTTP sockets and kills/reaps the isolated DNS resolver;
    asyncio.run retains normal executor shutdown with no DNS thread to join.
    """
    import httpx
    if deadline - time.monotonic() <= 0:
        return False
    headers = {'Authorization': f'Bearer {observed.runtime_api_key}'}
    if provider == 'anthropic':
        # The real OAuth client's wire (betas, detected Claude Code user-agent, x-app): a
        # drifted proof wire would be refused and read as a false "still walled".
        from agent.anthropic_adapter import _CLAUDE_CODE_SYSTEM_PREFIX, oauth_wire_headers
        url = 'https://api.anthropic.com/v1/messages'
        headers.update({'anthropic-version': '2023-06-01', **oauth_wire_headers()})
        body = {'model': model, 'max_tokens': 1,
                'system': [{'type': 'text', 'text': _CLAUDE_CODE_SYSTEM_PREFIX}],
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
        return asyncio.run(_run_attempt(provider, url, headers, body, deadline))
    except (httpx.HTTPError, OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        return False


def _proof_gate_path():
    from hermes_cli.kanban_db import kanban_home
    return str(kanban_home() / 'quota_proof_gate.db')


def _proof_key(provider, model, entry):
    aid = getattr(entry, 'id', None)
    return json.dumps([provider, model, aid]) if isinstance(aid, str) and aid else None


def _gate_connect(path):
    import sqlite3
    # quota_proof_gate.db: a local per-home proof throttle, never a board.
    conn = sqlite3.connect(path, timeout=0.25)  # noqa: kanban-lockguard (not a board)
    conn.execute('CREATE TABLE IF NOT EXISTS proofs (key TEXT PRIMARY KEY, at REAL)')
    conn.commit()
    return conn


def _reserve_proofs(provider, model, entries, now, *, path=None):
    """Entries whose proof may run now, each reserved for the cooldown window (cross-process).

    An account with a failed or in-flight proof within FAILED_PROOF_COOLDOWN_SECONDS is skipped,
    so concurrent and back-to-back starts share one probe. A verified proof deletes its row.
    Entries without a pool row id are not gated. An unusable store fails open (probe as before).
    """
    from contextlib import closing
    keyed = [(e, _proof_key(provider, model, e)) for e in entries]
    try:
        with closing(_gate_connect(path or _proof_gate_path())) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM proofs WHERE at < ?', (now - FAILED_PROOF_COOLDOWN_SECONDS,))
            allowed = []
            for entry, key in keyed:
                if key is not None:
                    if conn.execute('SELECT 1 FROM proofs WHERE key=?', (key,)).fetchone():
                        continue
                    conn.execute('INSERT OR REPLACE INTO proofs VALUES (?, ?)', (key, now))
                allowed.append(entry)
            return allowed
    except Exception:  # health: allow BLE001 -- a broken throttle must not disable recovery
        import logging
        logging.getLogger(__name__).debug('quota proof gate unavailable; probing ungated', exc_info=True)
        return list(entries)


def _release_proof(provider, model, entry, *, path=None):
    """A verified proof clears its reservation so the next bench of this account probes at once."""
    from contextlib import closing
    key = _proof_key(provider, model, entry)
    if key is None:
        return
    with contextlib.suppress(Exception), closing(_gate_connect(path or _proof_gate_path())) as conn, conn:
        conn.execute('DELETE FROM proofs WHERE key=?', (key,))


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
    Each (provider, model, account) proof is reserved for FAILED_PROOF_COOLDOWN_SECONDS across
    processes (released only by a completed proof), so a real wall costs one completion per
    account per window, not one per Kanban start.
    """
    from copy import deepcopy
    from agent.credential_pool import load_pool
    from agent.credential_pool_disabled import disabled_keys
    from hermes_cli.kanban_quota_probe import PROBE_DEADLINE_SECONDS
    if provider == 'anthropic':
        # Resolve the cached Claude Code version (a `claude --version` subprocess) before the
        # proof deadline starts, not inside every proof thread.
        from agent.anthropic_adapter import _get_claude_code_version
        _get_claude_code_version()
    deadline = time.monotonic() + (PROBE_DEADLINE_SECONDS if seconds is None else seconds)
    if provider not in ('anthropic', 'openai-codex') or not model:
        return False
    pool = load_pool(provider, allowed_auth_types={'oauth'})
    disabled = disabled_keys(provider)
    entries = [deepcopy(e) for e in pool.entries() if _eligible(e, provider, model, time.time(), disabled)]
    if entries:
        entries = _reserve_proofs(provider, model, entries, time.time())
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
                    _release_proof(provider, model, observed)
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
