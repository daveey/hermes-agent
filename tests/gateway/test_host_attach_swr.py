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

    def hung_identify(dialled, **kw):  # returns None like a timed-out client
        hang.wait(1.0)  # accepts, never answers: the caller's budget is spent waiting

    monkeypatch.setattr(control_socket, "identify_gateway", hung_identify)
    yield hang
    hang.set()


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
    time.sleep(0.1)  # memo now expired
    spent = _spent(2)
    assert spent < 0.5, spent
    assert host_attach.host_gateway() is first or host_attach.host_gateway() is not None


def test_cli_mode_keeps_synchronous_probe(silent_gateway, monkeypatch):
    monkeypatch.setattr(host_attach, "HOST_GATEWAY_CACHE_TTL_S", 0.05)
    host_attach.host_gateway()
    time.sleep(0.1)
    assert _spent(1) >= 0.9  # lifecycle/CLI callers still get a fresh answer


def test_stale_memo_older_than_cap_is_reprobed_synchronously(silent_gateway, monkeypatch):
    host_attach.enable_stale_while_revalidate()
    monkeypatch.setattr(host_attach, "HOST_GATEWAY_CACHE_TTL_S", 0.05)
    monkeypatch.setattr(host_attach, "HOST_GATEWAY_MAX_STALE_S", 0.1)
    host_attach.host_gateway()
    time.sleep(0.2)
    assert _spent(1) >= 0.9


def test_control_socket_identify_not_queued_behind_default_executor():
    assert control_socket._control_executor() is control_socket._control_executor()
