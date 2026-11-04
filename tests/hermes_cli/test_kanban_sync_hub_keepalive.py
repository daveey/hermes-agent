"""Hub requests reuse one kept-alive connection per thread (no TLS handshake + trust evaluation per request).

Exercised against a real local HTTP/1.1 server that counts accepted connections and requests, so the wire behaviour
(reuse, stale-socket recovery, no duplicate POST, the urlopen error contract) is what the tests see.
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import threading
import urllib.error

import pytest

from hermes_cli import kanban_sync_client as ksc


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.connections = 0
        self.requests: list[tuple[str, str]] = []
        self.mode = "ok"  # ok | close | drop_post | status:<n> | bad_status
        self.lock = threading.Lock()
        self.dropped = threading.Event()
        self.open_sockets: list[socket.socket] = []

    def get_request(self):
        sock, addr = super().get_request()
        with self.lock:
            self.connections += 1
            self.open_sockets.append(sock)
        return sock, addr

    def close_idle(self):
        """Server-side idle close of every kept-alive socket (what an edge does after its idle timeout)."""
        with self.lock:
            socks, self.open_sockets = self.open_sockets, []
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _serve(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        srv = self.server
        with srv.lock:
            srv.requests.append((self.command, self.path))
        mode = srv.mode
        if mode in ("drop_post", "drop_once") and self.command == "POST":
            if mode == "drop_once":
                srv.mode = "ok"
                srv.dropped.set()
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if mode == "bad_status":
            self.wfile.write(b"garbage\r\n\r\n")
            self.close_connection = True
            return
        status = int(mode.split(":")[1]) if mode.startswith("status:") else 200
        payload = json.dumps({"path": self.path, "body": body.decode() or None}).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        if mode == "close":
            self.send_header("connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _serve


@pytest.fixture
def server(monkeypatch):
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    srv = _Server()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _hub(srv, prefix=""):
    host, port = srv.server_address
    return ksc.Hub(ksc.Identity(url=f"http://{host}:{port}{prefix}", key="k" * 16, gateway="gw"), timeout=5)


def test_sequential_requests_share_one_connection(server):
    hub = _hub(server)
    for i in range(5):
        assert hub.changes("default", since=i)["path"] == f"/v2/boards/default/changes?since={i}&limit=500"
    hub.ops("default", [{"op": 1}])
    assert len(server.requests) == 6
    assert server.connections == 1  # urlopen opened 6: one TCP connect + TLS handshake + trust evaluation each


def test_url_path_prefix_is_kept(server):
    hub = _hub(server, prefix="/base")
    assert hub.machines()["path"] == "/base/v2/machines"


def test_server_idle_close_reconnects_transparently(server):
    hub = _hub(server)
    hub.machines()
    server.close_idle()
    assert hub.machines()["path"] == "/v2/machines"
    assert server.connections == 2
    assert len(server.requests) == 2  # the closed socket was detected before reuse; nothing was sent twice


def test_stale_socket_get_is_retried_once(server, monkeypatch):
    hub = _hub(server)
    hub.machines()
    monkeypatch.setattr(ksc, "_peer_closed", lambda conn: False)  # the close lands after the pre-reuse check
    server.close_idle()
    assert hub.machines()["path"] == "/v2/machines"
    assert server.connections == 2


@pytest.mark.parametrize("elapsed", [1.5, 2.1])
@pytest.mark.parametrize("reserve", [False, True])
def test_get_resend_recomputes_remaining_budget(server, monkeypatch, elapsed, reserve):
    """A real reused connection closes after spending part (or all) of the budget."""
    from types import SimpleNamespace
    clock = [100.0]
    monkeypatch.setattr(ksc, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    hub = _hub(server)
    hub.machines()
    hub.deadline = 102.0
    original = _Handler._serve
    method = "do_POST" if reserve else "do_GET"

    def request():
        return (hub.reserve("default", task_id="t_test", expected_revision=7, candidates=["worker-1"])
                if reserve else hub.machines())

    def close_once(handler):
        clock[0] += elapsed
        monkeypatch.setattr(_Handler, method, original)
        handler.close_connection = True
        handler.connection.shutdown(socket.SHUT_RDWR)

    monkeypatch.setattr(_Handler, method, close_once)
    try:
        if elapsed > 2:
            with pytest.raises(TimeoutError, match="budget exhausted"):
                request()
            assert server.connections == 1
        else:
            expected_path = "/v2/boards/default/placement/reserve" if reserve else "/v2/machines"
            assert request()["path"] == expected_path
            assert hub._local.conn.sock.gettimeout() <= 0.5
            assert server.connections == 2
    finally:
        hub.close()


def test_stale_socket_reserve_is_retried_once(server, monkeypatch):
    hub = _hub(server)
    hub.machines()
    monkeypatch.setattr(ksc, "_peer_closed", lambda conn: False)
    # Half-close emits FIN but still accepts the request writes, deterministically
    # exercising getresponse rather than the existing request-write retry.
    server.mode = "drop_once"
    send = hub._local.conn.request

    def send_then_observe_drop(*args, **kwargs):
        send(*args, **kwargs)
        assert server.dropped.wait(2)  # first handler switches mode before the retry can arrive

    monkeypatch.setattr(hub._local.conn, "request", send_then_observe_drop)
    with server.lock:
        for sock in server.open_sockets:
            sock.shutdown(socket.SHUT_WR)
    try:
        result = hub.reserve("default", task_id="t_test", expected_revision=7, candidates=["worker-1"])
        assert json.loads(result["body"])["expected_revision"] == 7
        assert server.connections == 2
        assert [m for m, _ in server.requests].count("POST") == 2
    finally:
        hub.close()


@pytest.mark.parametrize("warm, expected", [(True, 2), (False, 1)])
def test_reserve_lost_answer_retry_is_bounded(server, warm, expected):
    hub = _hub(server)
    if warm:
        hub.machines()
    server.mode = "drop_post"
    try:
        with pytest.raises(OSError):
            hub.reserve("default", task_id="t_test", expected_revision=7, candidates=["worker-1"])
        assert [m for m, _ in server.requests].count("POST") == expected
        assert server.connections == expected
    finally:
        hub.close()


def test_reserve_lost_answer_replays_identical_body(server, monkeypatch):
    hub = _hub(server)
    hub.machines()
    bodies = []
    original = _Handler.do_POST

    def lose_once(handler):
        bodies.append(handler.rfile.read(int(handler.headers["content-length"])))
        monkeypatch.setattr(_Handler, "do_POST", original)
        handler.close_connection = True
        handler.connection.shutdown(socket.SHUT_RDWR)

    monkeypatch.setattr(_Handler, "do_POST", lose_once)
    try:
        result = hub.reserve("default", task_id="t_test", expected_revision=7, candidates=["worker-1"])
        assert bodies == [result["body"].encode()]
        assert server.connections == 2
    finally:
        hub.close()


def test_lease_whose_answer_is_lost_is_not_resent(server):
    hub = _hub(server)
    hub.machines()
    server.mode = "drop_post"
    try:
        with pytest.raises(OSError):
            hub.leases("default", {"op": "claim"})
        assert [m for m, _ in server.requests].count("POST") == 1
    finally:
        hub.close()


def test_post_whose_answer_is_lost_is_not_resent(server):
    hub = _hub(server)
    hub.machines()
    server.mode = "drop_post"
    with pytest.raises(OSError):
        hub.ops("default", [{"op": 1}])
    assert [m for m, _ in server.requests].count("POST") == 1
    server.mode = "ok"
    assert hub.ops("default", [{"op": 2}])["body"] == json.dumps({"ops": [{"op": 2}]})


def test_error_statuses_keep_the_urlopen_contract_and_the_connection(server):
    hub = _hub(server)
    server.mode = "status:410"
    with pytest.raises(ksc.Resync):
        hub.changes("default", since=1)
    server.mode = "status:503"
    with pytest.raises(ksc.HubError) as exc:
        hub.machines()
    assert exc.value.status == 503 and '"path": "/v2/machines"' in exc.value.detail
    server.mode = "ok"
    hub.machines()
    assert server.connections == 1


def test_protocol_garbage_is_an_oserror(server):
    hub = _hub(server)
    server.mode = "bad_status"
    with pytest.raises(urllib.error.URLError):
        hub.machines()
    server.mode = "ok"
    assert hub.machines()["path"] == "/v2/machines"


def test_connection_close_response_is_not_reused(server):
    hub = _hub(server)
    server.mode = "close"
    hub.machines()
    hub.machines()
    assert server.connections == 2


def test_idle_connection_past_reuse_window_is_replaced(server, monkeypatch):
    hub = _hub(server)
    hub.machines()
    monkeypatch.setattr(ksc.Hub, "IDLE_REUSE_S", -1.0)
    hub.machines()
    assert server.connections == 2


def test_threads_never_share_a_connection(server):
    hub = _hub(server)
    errors = []

    def work():
        try:
            for _ in range(5):
                hub.machines()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(server.requests) == 20
    assert server.connections == 4


def test_environment_proxy_keeps_urlopen(server, monkeypatch):
    calls = []
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    monkeypatch.setattr(ksc.Hub, "_call_urlopen", lambda self, m, p, d, *a: calls.append((m, p)) or {"via": "urlopen"})
    assert _hub(server).machines() == {"via": "urlopen"}
    assert calls == [("GET", "/v2/machines")] and server.requests == []


def test_board_deadline_caps_every_kept_alive_request(server, monkeypatch):
    """The worker-placement request budget (``deadline``) caps a reused socket too, not only the first connect."""
    from types import SimpleNamespace
    clock = [100.0]
    monkeypatch.setattr(ksc, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    host, port = server.server_address
    hub = ksc.Hub(ksc.Identity(url=f"http://{host}:{port}", key="k" * 16, gateway="gw"), timeout=5, deadline=104.0)
    hub.machines()
    assert hub._local.conn.sock.gettimeout() == 4.0
    clock[0] = 102.5
    hub.machines()
    assert server.connections == 1  # reused, yet bound by the remaining budget
    assert hub._local.conn.sock.gettimeout() == 1.5
    clock[0] = 104.5
    with pytest.raises(TimeoutError, match="budget exhausted"):
        hub.machines()
    assert len(server.requests) == 2


def test_failed_send_on_reused_socket_is_retried_on_a_fresh_one(server, monkeypatch):
    hub = _hub(server)
    hub.machines()
    monkeypatch.setattr(ksc, "_peer_closed", lambda conn: False)
    hub._local.conn.sock.close()  # the reused socket is dead before the request is written
    assert hub.ops("default", [{"op": 1}])["body"] == json.dumps({"ops": [{"op": 1}]})
    assert [m for m, _ in server.requests].count("POST") == 1
    assert server.connections == 2


def test_connection_inherited_across_fork_is_left_alone(server):
    hub = _hub(server)
    hub.machines()
    inherited = hub._local.conn
    hub._local.pid = -1  # what a forked child sees: the parent's pid on the connection
    hub.machines()
    assert hub._local.conn is not inherited and inherited.sock is not None  # not closed under the parent
    assert server.connections == 2


def test_peer_closed_works_above_select_fd_ceiling():
    import resource
    from types import SimpleNamespace
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft <= 1100:
        pytest.skip("RLIMIT_NOFILE too low to hold an fd above 1024")
    ours, peer = socket.socketpair()
    high = socket.socket(fileno=os.dup2(ours.fileno(), 1100))
    ours.close()
    try:
        conn = SimpleNamespace(sock=high)
        assert ksc._peer_closed(conn) is False  # select() raises ValueError for fd 1100: every reuse reconnected
        peer.close()
        assert ksc._peer_closed(conn) is True
    finally:
        high.close()
        peer.close()


class _Redirect(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _serve(self):
        n = int(self.headers.get("content-length") or 0)
        if n:
            self.rfile.read(n)
        self.server.requests.append((self.command, self.path))
        if self.path.startswith("/moved"):
            payload = b'{"moved": true}'
            self.send_response(200)
        else:
            payload = b""
            self.send_response(302)
            self.send_header("location", "/moved")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _serve


@pytest.mark.parametrize("proxied", [False, True])
@pytest.mark.parametrize("step", [0.4, 2.1])
def test_redirect_chain_recomputes_budget(server, monkeypatch, proxied, step):
    """Every actual HTTP send, including urllib's own follows, gets remaining time."""
    import http.client
    from types import SimpleNamespace
    clock = [100.0]
    monkeypatch.setattr(ksc, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(ksc.Hub, "_proxied", staticmethod(lambda parts: proxied))
    sent = []
    request = http.client.HTTPConnection.request

    def observed_request(conn, *args, **kwargs):
        sent.append((clock[0], conn.timeout))
        return request(conn, *args, **kwargs)

    def redirect(handler):
        clock[0] += step
        server.requests.append((handler.command, handler.path))
        handler.send_response(302 if handler.path != "/end" else 200)
        handler.send_header("location", "/end" if handler.path == "/hop" else "/hop")
        handler.send_header("content-length", "2" if handler.path == "/end" else "0")
        handler.end_headers()
        if handler.path == "/end":
            handler.wfile.write(b"{}")

    monkeypatch.setattr(http.client.HTTPConnection, "request", observed_request)
    monkeypatch.setattr(_Handler, "do_GET", redirect)
    hub = _hub(server)
    hub.deadline = 102.0
    try:
        if step > 2:
            with pytest.raises(TimeoutError, match="budget exhausted"):
                hub.machines()
            assert len(sent) == 1
        else:
            assert hub.machines() == {}
            assert len(sent) >= 3
            for now, timeout in sent:
                assert 0 < timeout <= 102.0 - now
    finally:
        hub.close()


def test_redirects_follow_for_get_and_never_resend_a_post(monkeypatch):
    for var in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)
    srv.daemon_threads = True
    srv.requests = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        hub = _hub(srv)
        assert hub.machines() == {"moved": True}
        srv.requests.clear()
        with pytest.raises(ksc.HubError) as exc:
            hub.ops("default", [{"op": 1}])
        assert exc.value.status == 302
        assert srv.requests == [("POST", "/v2/boards/default/ops")]
    finally:
        srv.shutdown()
        srv.server_close()
