"""A dashboard must not re-pay a hung gateway's ``identify`` timeout on every request."""
import json
import os
import socket
import threading
import time

import pytest

from gateway import control_socket, host_attach
from gateway import host_rendezvous as hr
from hermes_constants import get_hermes_home


@pytest.fixture(autouse=True)
def _cold(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    host_attach.invalidate_host_gateway_cache()
    yield
    host_attach.enable_stale_while_revalidate(False)
    host_attach.invalidate_host_gateway_cache()


@pytest.fixture
def silent_gateway(monkeypatch):
    """A live owner whose control socket accepts connections and never answers."""
    home = get_hermes_home()
    assert hr.claim_host_lock(hr.ROLE_GATEWAY)[0] is hr.HostLockOutcome.ACQUIRED
    pid = os.getpid()
    payload = {"role": hr.ROLE_GATEWAY, "home": str(home), "pid": pid,
               "createTime": hr.process_create_time(pid), "host": "", "port": None,
               "protocolVersion": hr.HOST_PROTOCOL_VERSION, "tokenFingerprint": "",
               "profiles": [], "updatedAt": ""}
    path = hr.record_path(hr.ROLE_GATEWAY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    host_attach.invalidate_host_gateway_cache()

    hang = threading.Event()
    accepted = threading.Event()
    # Use the real client and a real accepting socket, not a mocked identify call.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as root:
        address = Path(root) / "silent.sock"
        monkeypatch.setattr(control_socket, "resolve_client_socket_path", lambda home: address)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(address))
        listener.listen()
        listener.settimeout(0.05)
        peers = []

        def accept():
            while not hang.is_set():
                try:
                    peer, _ = listener.accept()
                    peers.append(peer)
                    accepted.set()
                except TimeoutError:
                    continue

        worker = threading.Thread(target=accept, daemon=True)
        worker.start()
        try:
            yield accepted
        finally:
            hang.set()
            worker.join(timeout=1)
            listener.close()
            for peer in peers:
                peer.close()


def _spent(n=2):
    t = time.monotonic()
    for _ in range(n):
        host_attach.host_gateway()
    return time.monotonic() - t


def test_dashboard_mode_serves_stale_memo_while_reprobing(silent_gateway, monkeypatch):
    host_attach.enable_stale_while_revalidate()
    monkeypatch.setattr(host_attach, "HOST_GATEWAY_CACHE_TTL_S", 0.05)
    first = host_attach.host_gateway()  # the one probe that pays the hang
    assert first is not None and first.served_known is False
    time.sleep(0.1)  # health: allow HX013 -- memo TTL expiry is the behaviour under test
    spent = _spent(2)
    assert spent < 0.5, spent
    assert host_attach.host_gateway() is first or host_attach.host_gateway() is not None


def test_cli_mode_keeps_synchronous_probe(silent_gateway, monkeypatch):
    monkeypatch.setattr(host_attach, "HOST_GATEWAY_CACHE_TTL_S", 0.05)
    host_attach.host_gateway()
    time.sleep(0.1)  # health: allow HX013 -- memo TTL expiry is the behaviour under test
    assert _spent(1) >= 0.9  # lifecycle/CLI callers still get a fresh answer


def test_stale_memo_older_than_cap_is_reprobed_synchronously(silent_gateway, monkeypatch):
    host_attach.enable_stale_while_revalidate()
    monkeypatch.setattr(host_attach, "HOST_GATEWAY_CACHE_TTL_S", 0.05)
    monkeypatch.setattr(host_attach, "HOST_GATEWAY_MAX_STALE_S", 0.1)
    host_attach.host_gateway()
    time.sleep(0.2)  # health: allow HX013 -- memo TTL expiry is the behaviour under test
    assert _spent(1) >= 0.9


def test_profiles_http_requests_do_not_repay_silent_socket(silent_gateway, monkeypatch):
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from hermes_cli import web_server_app
    from hermes_cli.web_routers.profiles import router

    @asynccontextmanager
    async def no_services(app):
        yield

    from hermes_cli import profiles
    named = get_hermes_home() / "profiles" / "probe"
    named.mkdir(parents=True)
    (named / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_iter_named_profile_dirs", lambda: [named])
    monkeypatch.setattr(web_server_app, "standalone_lifespan", no_services)
    monkeypatch.setattr(host_attach, "HOST_GATEWAY_CACHE_TTL_S", 0.05)
    app = FastAPI(lifespan=web_server_app.app_lifespan)
    app.include_router(router)
    with TestClient(app) as client:
        host_attach.host_gateway()  # prime once with the real socket's timeout
        assert silent_gateway.is_set()
        spans = []
        original = host_attach.host_gateway

        def measured(**kwargs):
            start = time.monotonic()
            try:
                return original(**kwargs)
            finally:
                spans.append(time.monotonic() - start)

        monkeypatch.setattr(host_attach, "host_gateway", measured)
        for _ in range(2):
            time.sleep(0.1)  # health: allow HX013 -- memo TTL expiry is the behaviour under test
            response = client.get("/api/profiles")
            assert response.status_code == 200
            assert response.json()["profiles"]
        assert len(spans) >= 2  # not vacuously testing a cached HTTP response
        assert sum(spans) < 0.5, spans


def test_control_socket_identify_not_queued_behind_default_executor(tmp_path):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        release = threading.Event()
        occupied = threading.Event()

        def block():
            occupied.set()
            release.wait(5)

        pending = loop.run_in_executor(None, block)
        server = control_socket.GatewayControlServer(
            tmp_path, verb_handlers={"identify": lambda: {"pid": 4242}})
        assert await server.start()
        try:
            while not occupied.is_set():
                await asyncio.sleep(0)
            path = control_socket.resolve_client_socket_path(tmp_path)
            reader, writer = await asyncio.open_unix_connection(str(path))
            try:
                writer.write(b'{"verb":"identify"}\n')
                await writer.drain()
                answer = await asyncio.wait_for(reader.readline(), timeout=0.5)
                assert json.loads(answer)["result"]["pid"] == 4242
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            release.set()
            await pending
            await server.stop()

    asyncio.run(scenario())
