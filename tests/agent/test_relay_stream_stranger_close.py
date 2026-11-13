"""[daveey] Relay-managed streams must not close a provider stream its chunk reader is still reading.

Sibling of test_aux_abandoned_provider_tls_fd.py / test_aux_stranger_close_siblings.py. ``ManagedLlmStream``
pulls each provider chunk on an ``asyncio.to_thread`` worker, and Relay pulls chunk N+1 before it hands over
chunk N, so a live stream nearly always has a worker blocked in ``SSL_read``. Closing the managed stream
(a /stop, a retry, a consumer that stops early) cancels that read, and the provider generator's ``finally``
closed the raw provider stream from the loop thread while the worker still read it. httpx then closed the
socket from a stranger thread: the fd number went to the next ``open()`` (a SQLite file) and the worker's
``SSL_read`` wrote a TLS alert into its header.
"""
from __future__ import annotations

import os
import threading

import httpx
import pytest

pytest.importorskip("nemo_relay")

from agent import relay_llm, relay_runtime
from tests.agent.test_aux_abandoned_provider_tls_fd import SQLITE_MAGIC, stalling_stream
from tests.agent.test_aux_stranger_close_siblings import _header_after_fd_reuse


@pytest.fixture
def tls_stream(request):
    """The stalling loopback TLS server (by name: a same-named parameter would shadow the fixture)."""
    assert stalling_stream
    return request.getfixturevalue("stalling_stream")


@pytest.fixture
def relay_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    lease = relay_runtime.SESSION_COORDINATOR.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(), session_id="session-1", platform="cli")
    turn = relay_runtime.SESSION_COORDINATOR.begin_turn(lease, turn_id="turn-1", task_id="task-1")
    lease.host.retain_managed_execution("test.relay_stream_stranger_close")
    try:
        yield
    finally:
        lease.host.release_managed_execution("test.relay_stream_stranger_close")
        relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")
        relay_runtime.SESSION_COORDINATOR.release_conversation(lease)
        relay_runtime._reset_for_tests()


def _fd_identity(fd: int) -> tuple[int, int] | None:
    try:
        st = os.fstat(fd)
    except OSError:
        return None
    return st.st_dev, st.st_ino


class _ProviderStream:
    """An SDK ``Stream``-shaped provider stream: iterates the HTTP body, ``.response``, ``close()``."""

    def __init__(self, http: httpx.Client, url: str, seen: dict) -> None:
        self._seen = seen
        self.response = http.send(http.build_request("POST", url, content=b"{}"), stream=True)
        seen["fd"] = self.response.extensions["network_stream"].get_extra_info("socket").fileno()
        seen["fd_identity"] = _fd_identity(seen["fd"])
        self._body = self.response.iter_raw()
        self.closed_on: list[str] = []

    def __iter__(self):
        return self

    def __next__(self):
        self._seen["reader"] = threading.current_thread().name
        self._seen["reads"] += 1
        self._seen["reading"].set()
        try:
            return {"delta": next(self._body).decode()}
        finally:
            self._seen["returned"].set()

    def close(self) -> None:
        self.closed_on.append(threading.current_thread().name)
        self.response.close()


def test_closing_a_live_managed_stream_never_hands_the_readers_tls_fd_to_sqlite(
        relay_session, tls_stream, tmp_path, caplog):
    caplog.set_level("INFO", logger="agent.aux_provider_handoff")
    http = httpx.Client(verify=tls_stream.ssl_context, timeout=httpx.Timeout(20.0))
    seen: dict = {"reads": 0, "reading": threading.Event(), "returned": threading.Event()}
    provider: list[_ProviderStream] = []

    def open_stream(_request):
        provider.append(_ProviderStream(http, tls_stream.url, seen))
        return provider[0]

    stream = relay_llm.stream(
        {"model": "m", "messages": []}, open_stream, session_id="session-1", name="custom",
        model_name="m", finalizer=lambda: {"content": "first"},
        metadata={"api_mode": "custom", "api_request_id": "stranger-close"},
    )
    try:
        assert next(stream) == {"delta": "first"}
        # Relay pulled ahead: a worker is now blocked reading the stalled body.
        seen["reading"].clear()
        seen["returned"].clear()
        assert seen["reads"] >= 2 or seen["reading"].wait(15), "no worker read the next chunk"
        assert not seen["returned"].is_set(), "the reader should be blocked on the stalled body"
        stream.close()  # a /stop or retry abandoning the stream mid-read
        # Before the fix the loop thread closed the socket under the reader, and the next open() in this
        # process (an event loop's epoll fd in practice) already owned the reader's fd number.
        assert seen["returned"].is_set() or _fd_identity(seen["fd"]) == seen["fd_identity"], (
            "the reader's fd was released while it still read it")
        # close() returns once the reader unwound, so a caller's later client close meets a quiet socket.
        assert seen["returned"].is_set(), "close() returned while a worker still read the provider stream"
        header = _header_after_fd_reuse(tmp_path, seen["fd"], tls_stream, seen["returned"])
    finally:
        tls_stream.resume.set()
    assert header[:16] == SQLITE_MAGIC, f"TLS bytes landed in the SQLite header: {header!r}"
    # The loop thread only shut the socket down; the worker that read it closed the provider stream.
    assert provider[0].closed_on == [seen["reader"]]
    assert "tcp_shutdown=1" in caplog.text
    http.close()


def test_a_finished_managed_stream_still_closes_its_provider_stream(relay_session):
    closed: list[str] = []

    class _Done:
        def __init__(self) -> None:
            self._chunks = iter([{"delta": "a"}, {"delta": "b"}])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._chunks)

        def close(self) -> None:
            closed.append(threading.current_thread().name)

    stream = relay_llm.stream(
        {"model": "m", "messages": []}, lambda _r: _Done(), session_id="session-1", name="custom",
        model_name="m", finalizer=lambda: {"content": "ab"},
        metadata={"api_mode": "custom", "api_request_id": "finished-stream"},
    )
    assert list(stream) == [{"delta": "a"}, {"delta": "b"}]
    assert len(closed) == 1
