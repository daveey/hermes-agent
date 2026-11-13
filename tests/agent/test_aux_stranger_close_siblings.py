"""[daveey] Siblings of the abandoned-provider TLS fd close (test_aux_abandoned_provider_tls_fd.py).

The hub incident (a TLS alert written into ``inbox.db``'s header) came from one thread closing a client
whose socket another thread was still reading: the fd number went to a SQLite file and the reader's next
``SSL_read`` took ``SQLit`` from it. Two more paths had the same shape:

1. ``class_routed_async``: adapter clients (Anthropic, Codex, Bedrock, Gemini) run the sync SDK call on an
   ``asyncio.to_thread`` worker (Gemini: the create and each stream step); a cancelled await leaves that
   worker reading while the ``finally`` closed the per-call client from the loop thread.
2. ``_store_cached_client`` (Nous 401 refresh): replacing a shared cache entry closed the old client
   although another caller can be mid-request on it.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

from agent import auxiliary_class_routing as routing
from agent import auxiliary_client as aux
from agent.gemini_native_adapter import GeminiNativeClient
from tests.agent.test_aux_abandoned_provider_tls_fd import (  # stalling_stream: fixture, resolved by name below
    SQLITE_MAGIC, _fd_is_open, stalling_stream,
)


@pytest.fixture
def tls_stream(request):
    """The stalling loopback TLS server of the sibling suite (by name: a same-named parameter would shadow it)."""
    assert stalling_stream  # imported so pytest registers it in this module
    return request.getfixturevalue("stalling_stream")


def _stream_until_stalled(http: httpx.Client, url: str, seen: dict, reading: threading.Event) -> str:
    """A streamed provider read: records the socket fd and its thread, then blocks on the stalled body."""
    seen["thread"] = threading.current_thread()
    try:
        with http.stream("POST", url, content=b"{}") as response:
            seen["fd"] = response.extensions["network_stream"].get_extra_info("socket").fileno()
            for _chunk in response.iter_raw():
                reading.set()
        return "summary"
    finally:
        seen["returned"].set()


def _header_after_fd_reuse(tmp_path, fd: int, tls_stream, returned: threading.Event) -> bytes:
    """Open a SQLite database on ``fd`` if (and only if) it was released, let the reader wake, return the header."""
    database = tmp_path / "inbox.db"
    sqlite3.connect(database).executescript("CREATE TABLE t(x); INSERT INTO t VALUES (1);")
    db_fd = os.open(database, os.O_RDWR)
    reused = not _fd_is_open(fd)
    if reused:
        os.dup2(db_fd, fd)  # the kernel's lowest-free-fd choice, made deterministic
    try:
        tls_stream.resume.set()  # data arrives: the reader's poll wakes and it reads the fd again
        assert returned.wait(15), "the reader never returned"
        with open(database, "rb") as fh:
            return fh.read(32)
    finally:
        os.close(db_fd)
        if reused:
            os.close(fd)


class _AdapterSyncClient:
    """Stands in for ``AnthropicAuxiliaryClient``: OpenAI-shaped sync wrapper over a per-call httpx pool."""

    api_key = "test"

    def __init__(self, http: httpx.Client, url: str, seen: dict, reading: threading.Event) -> None:
        self._client = http
        self.base_url = url
        self.closed_on: list[str] = []
        create = lambda **_kwargs: _stream_until_stalled(http, url, seen, reading)
        self.chat = aux._ChatShim(type("_Completions", (), {"create": staticmethod(create)})())

    def close(self) -> None:
        self.closed_on.append(threading.current_thread().name)
        self._client.close()


def test_cancelled_async_class_call_never_hands_the_to_thread_workers_tls_fd_to_sqlite(
        tls_stream, tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="agent.aux_provider_handoff")
    http = httpx.Client(verify=tls_stream.ssl_context, timeout=httpx.Timeout(20.0))
    seen: dict = {"returned": threading.Event()}
    reading = threading.Event()
    sync_client = _AdapterSyncClient(http, tls_stream.url, seen, reading)

    monkeypatch.setattr(routing, "_class_request", lambda *args: ("class:deep", "owner", {}, None, "class:deep"))
    monkeypatch.setattr(routing, "_prepare", lambda *args: (
        {"provider": "anthropic", "model": "deep", "api_mode": "anthropic_messages", "base_url": ""}, sync_client, {}))
    # The real async wrapper and its to_thread adapter; only the relay/progress plumbing is reduced to the call.
    monkeypatch.setattr(aux, "_to_async_client", lambda client, model, **_: (
        aux.AsyncAnthropicAuxiliaryClient(client), model))

    async def relay(client, kwargs, *, create, **_):
        return await create(kwargs)

    async def acreate(client, kwargs, task=None, **_):
        return await client.chat.completions.create(**kwargs)

    monkeypatch.setattr(aux, "_relay_async_completion", relay)
    monkeypatch.setattr(aux, "_acreate_with_progress", acreate)

    async def owner() -> bool:
        call = asyncio.ensure_future(routing.class_routed_async(lambda *a, **k: None)(task="compression", messages=[]))
        assert await asyncio.to_thread(reading.wait, 15), "the to_thread worker never started streaming"
        call.cancel()  # a gateway turn / wait_for timeout cancelling the aux await
        try:
            await call
        except asyncio.CancelledError:
            return True
        return False

    loop = asyncio.new_event_loop()  # not asyncio.run: its executor shutdown would wait for the stalled worker
    try:
        assert loop.run_until_complete(owner())
        header = _header_after_fd_reuse(tmp_path, seen["fd"], tls_stream, seen["returned"])
    finally:
        tls_stream.resume.set()
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
    assert header[:16] == SQLITE_MAGIC, f"TLS bytes landed in the SQLite header: {header!r}"
    # The loop thread only shut the socket down; the worker that read it closed the client.
    assert sync_client.closed_on == [seen["thread"].name]
    assert "tcp_shutdown=1" in caplog.text
    assert http.is_closed


class _StallingGeminiClient(GeminiNativeClient):
    """A real ``GeminiNativeClient`` (own ``_http``, real ``close`` and ``_advance_stream_iterator``) whose stream
    yields one chunk without I/O, then reads the stalled TLS body on the next step."""

    def __init__(self, http: httpx.Client, url: str, seen: dict, reading: threading.Event) -> None:
        super().__init__(api_key="test", base_url=url, http_client=http)
        self.closed_on: list[str] = []

        def chunks():
            yield "first"  # step 1: handed back to the loop, so step 2 is a fresh run_in_thread call
            _stream_until_stalled(http, url, seen, reading)  # step 2: blocks on the stalled body
            yield "second"

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: chunks()))

    def close(self) -> None:
        self.closed_on.append(threading.current_thread().name)
        super().close()


def test_cancelled_gemini_stream_step_is_closed_by_the_worker_reading_it(tls_stream, tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="agent.aux_provider_handoff")
    http = httpx.Client(verify=tls_stream.ssl_context, timeout=httpx.Timeout(20.0))
    seen: dict = {"returned": threading.Event()}
    reading = threading.Event()
    sync_client = _StallingGeminiClient(http, tls_stream.url, seen, reading)

    monkeypatch.setattr(routing, "_class_request", lambda *args: ("class:deep", "owner", {}, None, "class:deep"))
    monkeypatch.setattr(routing, "_prepare", lambda *args: (
        {"provider": "gemini", "model": "deep", "api_mode": "chat_completions", "base_url": ""}, sync_client, {}))
    received: list = []

    async def relay(client, kwargs, *, create, **_):
        return await create(kwargs)

    async def acreate(client, kwargs, task=None, **_):  # the real _to_async_client: AsyncGeminiNativeClient
        async for chunk in await client.chat.completions.create(**kwargs, stream=True):
            received.append(chunk)

    monkeypatch.setattr(aux, "_relay_async_completion", relay)
    monkeypatch.setattr(aux, "_acreate_with_progress", acreate)

    async def owner() -> bool:
        call = asyncio.ensure_future(routing.class_routed_async(lambda *a, **k: None)(task="compression", messages=[]))
        assert await asyncio.to_thread(reading.wait, 15), "the stream step never started reading"
        call.cancel()  # mid-stream: the first chunk arrived, the second step's worker is blocked in the read
        try:
            await call
        except asyncio.CancelledError:
            return True
        return False

    loop = asyncio.new_event_loop()  # not asyncio.run: its executor shutdown would wait for the stalled worker
    try:
        assert loop.run_until_complete(owner())
        header = _header_after_fd_reuse(tmp_path, seen["fd"], tls_stream, seen["returned"])
    finally:
        tls_stream.resume.set()
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
    assert received == ["first"]
    assert header[:16] == SQLITE_MAGIC, f"TLS bytes landed in the SQLite header: {header!r}"
    # Closed once, directly, by the stream step's worker: never the loop thread, never another executor thread.
    assert sync_client.closed_on == [seen["thread"].name]
    assert "handed to 1 abandoned provider daemon(s) (tcp_shutdown=1)" in caplog.text  # GeminiNativeClient._http
    assert http.is_closed


def test_replacing_a_cached_client_never_closes_it_under_a_live_tls_read(tls_stream, tmp_path, monkeypatch):
    monkeypatch.setattr(aux, "_client_cache", {})
    http = httpx.Client(verify=tls_stream.ssl_context, timeout=httpx.Timeout(20.0))
    stale = OpenAI(api_key="stale", base_url=tls_stream.url, http_client=http, max_retries=0)
    key = ("home", "nous", False, "", "", "", (), False, "", "", "m", ())
    aux._store_cached_client(key, stale, "m")
    seen: dict = {"returned": threading.Event()}
    reading = threading.Event()

    def caller_b() -> None:
        try:
            _stream_until_stalled(http, tls_stream.url, seen, reading)
        except httpx.HTTPError:
            pass  # the server ends the stream without a terminating chunk

    reader = threading.Thread(target=caller_b, name="aux-caller-b", daemon=True)
    reader.start()  # another aux caller mid-request on the shared cached client
    assert reading.wait(15), "the reader never started streaming"

    fresh = object()
    aux._store_cached_client(key, fresh, "m")  # the Nous 401 refresh replacing the entry
    assert aux._client_cache[key][0] is fresh

    header = _header_after_fd_reuse(tmp_path, seen["fd"], tls_stream, seen["returned"])
    reader.join(15)
    assert header[:16] == SQLITE_MAGIC, f"TLS bytes landed in the SQLite header: {header!r}"
    # Dropped, not closed (like the FIFO overflow and _evict_cached_clients): its last user retires it.
    assert not http.is_closed
    stale.close()


def test_run_in_thread_returns_the_result_and_closes_on_the_caller():
    from agent import aux_provider_handoff as handoff
    closed = []

    async def call() -> int:
        with handoff.client_scope("client", closed.append):
            return await handoff.run_in_thread(lambda x: x + 1, 41)

    assert asyncio.run(call()) == 42
    assert closed == ["client"]


def test_run_in_thread_cancelled_before_the_worker_starts_neither_runs_nor_defers():
    from concurrent.futures import ThreadPoolExecutor

    from agent import aux_provider_handoff as handoff
    closed, ran = [], []
    gate = threading.Event()

    async def call() -> None:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        blocker = loop.run_in_executor(None, gate.wait, 15)  # the only worker is busy: ours stays queued
        with handoff.client_scope("client", closed.append):
            pending = asyncio.ensure_future(handoff.run_in_thread(ran.append, "ran"))
            await asyncio.sleep(0)
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
        gate.set()
        await blocker

    asyncio.run(call())
    assert closed == ["client"]  # nothing abandoned: closed at once on the caller
    assert ran == []
