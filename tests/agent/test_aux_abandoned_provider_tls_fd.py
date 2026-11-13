"""[daveey] A hard-cancelled protected aux call must not release the TLS fd its provider daemon still reads.

Hub incident 2026-11-13 09:01:27 (and 2026-11-11 on actions.sqlite3): a compression yield cancelled a
class-routed aux call; the owner's ``finally`` closed the per-call client while the
``hermes-protected-aux-provider`` daemon was blocked reading its TLS stream. The fd number went to a
SQLite file, and the daemon's next ``SSL_read`` read ``SQLit`` and wrote a TLS alert into the header.
This reproduces it end to end on a real loopback TLS 1.3 server.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
import os
import socket
import sqlite3
import ssl
import threading
from types import SimpleNamespace

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import httpx
import pytest
from truststore._ssl_constants import _original_SSLContext

from agent import aux_provider_handoff as handoff
from agent import auxiliary_class_routing as routing
from agent import auxiliary_client as aux
from agent import ssl_close_tripwire
from tests.waiting import wait_until

SQLITE_MAGIC = b"SQLite format 3\x00"


def _tls_material(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test loopback")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "loopback.pem", tmp_path / "loopback.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


@pytest.fixture
def stalling_stream(tmp_path):
    """Loopback TLS 1.3 server: one chunked response, first chunk at once, the rest only on ``resume``."""
    cert_path, key_path = _tls_material(tmp_path)
    server_ctx = _original_SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    server_ctx.load_cert_chain(cert_path, key_path)
    listener = socket.create_server(("127.0.0.1", 0))
    resume, stop = threading.Event(), threading.Event()

    def serve() -> None:
        raw, _ = listener.accept()
        try:
            with server_ctx.wrap_socket(raw, server_side=True) as conn:
                request = b""
                while b"\r\n\r\n" not in request:
                    request += conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                             b"transfer-encoding: chunked\r\n\r\n5\r\nfirst\r\n")
                resume.wait(30)
                for _ in range(20):  # keep the peer's poll waking until the test is done
                    conn.sendall(b"6\r\nsecond\r\n")
                    if stop.wait(0.05):
                        break
        except OSError:
            pass  # the client shut its side down: expected after the fix

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    client_ctx = _original_SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.load_verify_locations(cert_path)
    yield SimpleNamespace(url=f"https://127.0.0.1:{listener.getsockname()[1]}/v1/messages",
                          ssl_context=client_ctx, resume=resume)
    stop.set()
    resume.set()
    server.join(10)
    listener.close()


class _PerCallClient:
    """Stands in for ``AnthropicAuxiliaryClient``: a per-call SDK client whose ``close`` closes its own pool."""

    def __init__(self, http: httpx.Client) -> None:
        self._client = http
        self.closed_on: list[str] = []

    def close(self) -> None:
        self.closed_on.append(threading.current_thread().name)
        self._client.close()


def _provider_daemons() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "hermes-protected-aux-provider"]


def _fd_is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def test_cancelled_class_call_never_hands_the_daemons_tls_fd_to_sqlite(stalling_stream, tmp_path, monkeypatch,
                                                                       caplog):
    caplog.set_level("INFO", logger="agent.aux_provider_handoff")
    http = httpx.Client(verify=stalling_stream.ssl_context, timeout=httpx.Timeout(20.0))
    client = _PerCallClient(http)
    reading, cancel = threading.Event(), threading.Event()
    seen: dict = {}

    def provider(_kwargs):  # runs on the protected daemon, like a streamed Anthropic summary
        with http.stream("POST", stalling_stream.url, content=b"{}") as response:
            sock = response.extensions["network_stream"].get_extra_info("socket")
            seen["fd"] = sock.fileno()
            for chunk in response.iter_raw():
                seen.setdefault("chunks", []).append(chunk)
                reading.set()
        return "summary"

    monkeypatch.setattr(routing, "_class_request", lambda *args: ("class:deep", "owner", {}, None, "class:deep"))
    monkeypatch.setattr(routing, "_prepare", lambda *args: (
        {"provider": "anthropic", "model": "deep", "api_mode": "anthropic_messages"}, client, {}))
    monkeypatch.setattr(routing, "_complete", lambda task, runtime, client, kwargs:
                        aux._run_protected_sync_provider_call(provider, kwargs))
    outcome: dict = {}

    def owner() -> None:  # the compression host: protected, with a hard-cancel source
        try:
            with aux.aux_interrupt_protection(cancel_check=cancel.is_set):
                routing.class_routed_sync(lambda *a, **k: None)(task="compression", messages=[])
        except aux.AuxiliaryExplicitCancellation:
            outcome["cancelled"] = True

    before = set(_provider_daemons())
    owner_thread = threading.Thread(target=owner, name="compression-owner")
    owner_thread.start()
    assert reading.wait(15), "the provider daemon never started streaming"
    daemons = [t for t in _provider_daemons() if t not in before]
    cancel.set()  # the update pause's compression yield
    owner_thread.join(15)
    assert outcome == {"cancelled": True}

    # SQLite opens a database and the kernel hands it the lowest free fd; pin it to the socket's number
    # when (and only when) the owner released it, so the reuse is deterministic.
    database = tmp_path / "inbox.db"
    sqlite3.connect(database).executescript("CREATE TABLE t(x); INSERT INTO t VALUES (1);")
    db_fd = os.open(database, os.O_RDWR)
    target = seen["fd"]
    reused = not _fd_is_open(target)
    if reused:
        os.dup2(db_fd, target)
    try:
        stalling_stream.resume.set()  # data arrives: the daemon's read wakes
        for daemon in daemons:
            daemon.join(15)
        assert not any(d.is_alive() for d in daemons)
        with open(database, "rb") as fh:
            header = fh.read(32)
    finally:
        os.close(db_fd)
        if reused:
            os.close(target)
    assert header[:16] == SQLITE_MAGIC, f"TLS bytes landed in the SQLite header: {header!r}"
    # The owner only shut the socket down (waking the daemon at once); the daemon closed the client.
    assert client.closed_on == ["hermes-protected-aux-provider"]
    assert "tcp_shutdown=1" in caplog.text
    assert http.is_closed


def test_release_without_abandoned_daemon_closes_on_the_caller():
    closed = []
    with handoff.client_scope("client", closed.append):
        pass
    assert closed == ["client"]


def test_release_runs_on_the_abandoned_daemon_once_it_finishes():
    closed, threads = [], []
    attempt = handoff.ProviderAttempt()

    def close(client) -> None:
        closed.append(client)
        threads.append(threading.get_ident())

    with handoff.client_scope(object(), close):
        handoff.note_abandoned(attempt)
    assert closed == []
    daemon = threading.Thread(target=attempt.finish)
    daemon.start()
    daemon.join(5)
    assert len(closed) == 1 and threads == [daemon.ident]


def test_release_closes_at_once_when_the_daemon_already_finished():
    closed = []
    attempt = handoff.ProviderAttempt()
    attempt.finish()
    with handoff.client_scope("client", closed.append):
        handoff.note_abandoned(attempt)
    assert closed == ["client"]


def test_note_abandoned_outside_a_scope_is_a_no_op():
    handoff.note_abandoned(handoff.ProviderAttempt())


@pytest.fixture
def tripwire():
    assert ssl_close_tripwire.install()
    try:
        yield
    finally:
        ssl_close_tripwire.uninstall()


def _connected_tls_socket(stalling_stream):
    host, port = "127.0.0.1", int(stalling_stream.url.split(":")[2].split("/")[0])
    sock = stalling_stream.ssl_context.wrap_socket(socket.create_connection((host, port)), server_hostname=host)
    sock.sendall(b"POST /v1/messages HTTP/1.1\r\nhost: x\r\ncontent-length: 0\r\n\r\n")
    received = b""
    while b"first" not in received:  # headers + first chunk: the server now stalls
        received += sock.recv(4096)
    return sock


def test_tripwire_logs_a_stranger_thread_close_under_a_live_read(stalling_stream, tripwire, caplog):
    caplog.set_level("WARNING", logger="agent.ssl_close_tripwire")
    sock = _connected_tls_socket(stalling_stream)
    reading = threading.Event()

    def reader() -> None:
        reading.set()
        try:
            sock.recv(4096)
        except OSError:
            pass

    thread = threading.Thread(target=reader, name="tls-reader")
    thread.start()
    assert reading.wait(5)
    wait_until(lambda: sock.__dict__.get("_hermes_readers"), what="the reader to enter recv")  # wrapper sees it
    sock.shutdown(socket.SHUT_RDWR)  # wake the reader FD-safely: the test itself must never recycle a live fd
    thread.join(5)
    caplog.clear()
    # Simulate the bad order on bookkeeping only: a reader still registered while a stranger closes.
    sock.__dict__["_hermes_readers"] = {thread.ident: 1}
    sock.close()
    assert "closed by thread" in caplog.text and "tls-reader" not in caplog.text
    assert str(thread.ident) in caplog.text


def test_tripwire_is_quiet_for_an_owner_close(stalling_stream, tripwire, caplog):
    caplog.set_level("WARNING", logger="agent.ssl_close_tripwire")
    sock = _connected_tls_socket(stalling_stream)
    sock.close()
    assert caplog.text == ""


def test_tripwire_installs_only_when_enabled(monkeypatch):
    monkeypatch.delenv("HERMES_DEBUG_SSL_CLOSE", raising=False)
    assert ssl_close_tripwire.install_from_env() is False
    assert not hasattr(ssl.SSLSocket.recv, "__wrapped__")
