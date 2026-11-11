"""Socket attribution uses both endpoints, with Darwin-compatible process fallback."""
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli import kanban_comment_writer as ancestry
from hermes_cli.kanban_http_comment_writer import http_comment_writer

PEER = ("127.0.0.1", 40123)
SERVER = ("127.0.0.1", 8765)
SCOPE = {"hermes.raw_transport": (PEER, SERVER)}


@pytest.fixture(autouse=True)
def supported_platform(monkeypatch):
    monkeypatch.setattr("hermes_cli.kanban_http_comment_writer.sys", SimpleNamespace(platform="linux"))


@pytest.mark.parametrize("platform", ["win32", "freebsd13"])
def test_unsupported_platform_preserves_gui_default(monkeypatch, platform):
    monkeypatch.setattr("hermes_cli.kanban_http_comment_writer.sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(psutil, "net_connections", lambda **kw: pytest.fail("unsupported ancestry"))
    assert http_comment_writer(SCOPE) == ("human", None)


def connection(pid=42, local=PEER, remote=SERVER):
    return SimpleNamespace(pid=pid, laddr=local, raddr=remote)


def darwin(monkeypatch):
    monkeypatch.setattr("hermes_cli.kanban_http_comment_writer.sys", SimpleNamespace(platform="darwin"))


@pytest.mark.parametrize("global_result", [psutil.AccessDenied(), [], [connection(None)]])
def test_linux_never_scans_processes_for_an_unattributed_socket(monkeypatch, global_result):
    """Linux's global inventory already reads every socket it can; a scan finds nothing more."""
    def global_connections(**kwargs):
        if isinstance(global_result, Exception):
            raise global_result
        return global_result

    monkeypatch.setattr(psutil, "net_connections", global_connections)
    monkeypatch.setattr(psutil, "process_iter", lambda: pytest.fail("per-process scan on Linux"))
    assert http_comment_writer(SCOPE) == ("human", None)


@pytest.mark.parametrize("global_result", [psutil.AccessDenied(), [], [connection(None)]])
def test_process_fallback_resolves_despite_unrelated_denied_process(monkeypatch, global_result):
    darwin(monkeypatch)
    def global_connections(**kwargs):
        if isinstance(global_result, Exception):
            raise global_result
        return global_result

    def denied(**kwargs):
        raise psutil.AccessDenied(3)

    processes = [SimpleNamespace(pid=3, net_connections=denied),
                 SimpleNamespace(pid=42, net_connections=lambda **kw: [connection(None)])]
    monkeypatch.setattr(psutil, "net_connections", global_connections)
    monkeypatch.setattr(psutil, "process_iter", lambda: iter(processes))
    monkeypatch.setattr(ancestry, "agent_ancestor", lambda pid: f"{pid}:HERMES_AGENT")
    assert http_comment_writer(SCOPE) == ("agent", None)


@pytest.mark.parametrize("peers", [[connection(41), connection(42)], [connection(42), connection(41)]])
@pytest.mark.parametrize("inconclusive", [False, True])
def test_any_matching_owner_can_lower_provenance(monkeypatch, peers, inconclusive):
    monkeypatch.setattr(psutil, "net_connections", lambda **kw: peers)

    def classify(pid):
        if pid == 41:
            return None
        if inconclusive:
            raise ancestry.Inconclusive("exited")
        return "42:HERMES_AGENT"

    monkeypatch.setattr(ancestry, "agent_ancestor", classify)
    assert http_comment_writer(SCOPE) == ("agent", None)


@pytest.mark.parametrize("connections", [
    [], [connection(local=("127.0.0.2", PEER[1]))],
    [connection(remote=(SERVER[0], SERVER[1] + 1))],
    [connection(local=SERVER, remote=PEER)],
])
def test_unresolved_complete_reverse_tuple_keeps_legacy_human(monkeypatch, connections):
    monkeypatch.setattr(psutil, "net_connections", lambda **kw: connections)
    monkeypatch.setattr(psutil, "process_iter", lambda: iter([]))
    monkeypatch.setattr(ancestry, "agent_ancestor", lambda pid: pytest.fail("not this socket"))
    assert http_comment_writer(SCOPE) == ("human", None)


@pytest.mark.parametrize("scope", [{}, {"client": PEER, "server": SERVER},
    {"hermes.raw_transport": (("192.0.2.1", PEER[1]), SERVER)},
    {"hermes.raw_transport": (None, SERVER)},
    {"hermes.raw_transport": (PEER, None)},
    {"hermes.raw_transport": (("testclient", 1), SERVER)},
])
def test_only_raw_loopback_peer_is_eligible(monkeypatch, scope):
    monkeypatch.setattr(psutil, "net_connections", lambda **kw: pytest.fail("not local TCP"))
    assert http_comment_writer(scope) == ("human", None)


def test_resolved_clean_ancestry_is_human(monkeypatch):
    monkeypatch.setattr(psutil, "net_connections", lambda **kw: [connection()])
    monkeypatch.setattr(ancestry, "agent_ancestor", lambda pid: None)
    assert http_comment_writer(SCOPE) == ("human", None)


def test_ipv4_mapped_ipv6_matches_ipv4_socket(monkeypatch):
    monkeypatch.setattr(psutil, "net_connections", lambda **kw: [connection()])
    monkeypatch.setattr(ancestry, "agent_ancestor", lambda pid: "42:HERMES_AGENT")
    scope = {"hermes.raw_transport": (("::ffff:127.0.0.1", PEER[1]), ("::ffff:127.0.0.1", SERVER[1]))}
    assert http_comment_writer(scope) == ("agent", None)


@pytest.mark.parametrize("fallback", [False, True])
def test_real_loopback_socket_resolves_its_client_pid(monkeypatch, fallback):
    import os
    import socket

    seen = []

    def classify(pid):
        seen.append(pid)
        return f"{pid}:HERMES_AGENT"

    monkeypatch.setattr(ancestry, "agent_ancestor", classify)
    proc = psutil.Process(os.getpid())
    # Both inventory paths inspect real sockets, but only those this test owns.
    monkeypatch.setattr(psutil, "net_connections", lambda **kw: [
        connection(proc.pid, c.laddr, c.raddr) for c in proc.net_connections(**kw)])
    monkeypatch.setattr(psutil, "process_iter", lambda: iter([proc]))
    if fallback:
        darwin(monkeypatch)
        def denied(**kwargs):
            raise psutil.AccessDenied()
        monkeypatch.setattr(psutil, "net_connections", denied)
        # Retain real per-process socket inspection, without touching unrelated host processes.
        monkeypatch.setattr(psutil, "process_iter", lambda: iter([psutil.Process(os.getpid())]))
    with socket.socket() as listener, socket.socket() as client:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        client.connect(listener.getsockname())
        accepted, peer = listener.accept()
        with accepted:
            assert http_comment_writer({"hermes.raw_transport": (peer, accepted.getsockname())}) == ("agent", None)
    assert seen == [os.getpid()]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("error", [psutil.AccessDenied(), psutil.NoSuchProcess(3), OSError("unavailable")])
def test_unavailable_socket_inventories_keep_legacy_default(monkeypatch, error, platform):
    monkeypatch.setattr("hermes_cli.kanban_http_comment_writer.sys", SimpleNamespace(platform=platform))
    def unavailable(**kwargs):
        raise error
    monkeypatch.setattr(psutil, "net_connections", unavailable)
    monkeypatch.setattr(psutil, "process_iter", unavailable)
    assert http_comment_writer(SCOPE) == ("human", None)


def test_missing_psutil_keeps_legacy_default(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "psutil", None)
    assert http_comment_writer(SCOPE) == ("human", None)


def test_partial_global_inventory_checks_all_fallback_owners(monkeypatch):
    darwin(monkeypatch)
    monkeypatch.setattr(psutil, "net_connections", lambda **kw: [connection(41), connection(None)])
    processes = [SimpleNamespace(pid=pid, net_connections=lambda **kw: [connection(None)]) for pid in (41, 42)]
    monkeypatch.setattr(psutil, "process_iter", lambda: iter(processes))
    monkeypatch.setattr(ancestry, "agent_ancestor", lambda pid: None if pid == 41 else "42:HERMES_AGENT")
    assert http_comment_writer(SCOPE) == ("agent", None)
