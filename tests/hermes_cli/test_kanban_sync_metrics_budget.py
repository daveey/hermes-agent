"""A CPU-bearing metrics POST has a hard 2 s total budget (real local HTTP server, real sockets)."""
import http.server
import threading
import time

import pytest

from hermes_cli import kanban_sync_client as ksc

CPU = {"cpu_busy_pct": 12.5, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": 1_700_000_000_000}


class Server:
    def __init__(self, mode):
        self.requests = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                outer.requests.append((self.path, self.rfile.read(n)))
                if mode == "stall":
                    time.sleep(4)  # health: allow HX013 -- the stalled server is the behaviour under test
                    body = b"{}"
                    self.send_response(200)
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif mode == "trickle":  # headers fast, then one byte every 0.4 s: each recv is within its timeout
                    body = b'{"ok": true}' + b" " * 12
                    self.send_response(200)
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    for i in range(len(body)):
                        try:
                            self.wfile.write(body[i:i + 1])
                            self.wfile.flush()
                        except OSError:
                            return
                        time.sleep(0.4)  # health: allow HX013 -- trickling bytes is the behaviour under test
                else:
                    body = b'{"ok": true}'
                    self.send_response(200)
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server(request):
    s = Server(request.param)
    yield s
    s.close()


def hub(url):
    return ksc.Hub(ksc.Identity(url=url, key="k" * 16, gateway="gw"))


@pytest.mark.parametrize("server", ["stall", "trickle"], indirect=True)
def test_cpu_report_has_hard_total_budget(server, monkeypatch):
    monkeypatch.setattr(ksc.urllib.request, "getproxies", dict)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        hub(server.url).report_metrics({"cpus": 4, **CPU})
    assert time.monotonic() - t0 <= ksc.CPU_REPORT_BUDGET_S + 0.25
    assert len(server.requests) == 1  # sent once, never resent


@pytest.mark.parametrize("server", ["ok"], indirect=True)
def test_cpu_report_succeeds_inside_budget_and_plain_report_keeps_normal_path(server, monkeypatch):
    monkeypatch.setattr(ksc.urllib.request, "getproxies", dict)
    h = hub(server.url)
    assert h.report_metrics({"cpus": 4, **CPU}) == {"ok": True}
    assert h.report_metrics({"cpus": 4}) == {"ok": True}
    assert len(server.requests) == 2


def test_cpu_report_budget_is_two_seconds():
    assert ksc.CPU_REPORT_BUDGET_S == 2.0


@pytest.mark.parametrize("elapsed,reason,exhausted", [
    (1.0, TimeoutError("timed out"), True),
    (0.5, TimeoutError("timed out"), False),
    (1.0, ConnectionResetError("reset"), False),
])
def test_bounded_call_worker_timeout_wins_race(monkeypatch, elapsed, reason, exhausted):
    """Both deadline contenders must expose the same timeout, without hiding transport failures."""
    from types import SimpleNamespace
    import urllib.error

    clock = [100.0]
    error = urllib.error.URLError(reason)
    real_thread = threading.Thread

    class FinishedWorker(real_thread):
        def join(self, timeout=None):
            # Select the worker-wins ordering regardless of host scheduling.
            super().join(10)
            assert not self.is_alive()

    def fail(self, *args):
        clock[0] += elapsed
        raise error

    monkeypatch.setattr(ksc, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(ksc.Hub, "_call", fail)
    monkeypatch.setattr(ksc._hub_threading, "Thread", FinishedWorker)
    with pytest.raises(TimeoutError if exhausted else urllib.error.URLError) as caught:
        ksc._bounded_call(ksc.Identity(url="http://unused", key="k" * 16, gateway="gw"),
                          "POST", "/metrics", {}, 1.0)
    if exhausted:
        assert caught.value.__cause__ is error
    else:
        assert caught.value is error


@pytest.mark.parametrize("server", ["stall", "trickle"], indirect=True)
def test_abandoned_bounded_call_thread_ends_promptly_after_the_budget(server, monkeypatch):
    """HX012 trace: a trickling response keeps every recv inside its socket timeout, so the abandoned request thread
    is not ended by a timeout. The caller's expiry must end it (and close its socket), or one thread and connection
    leaks per 10 s report for as long as the server trickles."""
    monkeypatch.setattr(ksc.urllib.request, "getproxies", dict)
    before = {t for t in threading.enumerate() if t.name == "kanban-bounded-call"}
    with pytest.raises(TimeoutError):
        ksc._bounded_call(ksc.Identity(url=server.url, key="k" * 16, gateway="gw"), "POST",
                          "/v2/machines/me/metrics", {"cpus": 4, **CPU}, 1.0)
    expired = time.monotonic()
    left = [t for t in threading.enumerate() if t.name == "kanban-bounded-call" and t not in before]
    for t in left:
        t.join(1.0)
    assert not any(t.is_alive() for t in left), "abandoned request thread outlived the budget by > 1 s"
    assert time.monotonic() - expired <= 1.0


@pytest.mark.parametrize("server", ["trickle"], indirect=True)
def test_connect_finishing_after_expiry_cuts_its_own_socket(server, monkeypatch):
    """Race arm: the budget expires while the request thread is still connecting (no socket for the caller to cut).
    The connect must then shut its new socket itself, so the trickle cannot keep the thread alive."""
    import http.client
    monkeypatch.setattr(ksc.urllib.request, "getproxies", dict)
    real_connect = http.client.HTTPConnection.connect

    def slow_connect(self):
        time.sleep(1.3)  # health: allow HX013 -- a connect still in progress when the 1 s budget expires
        real_connect(self)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", slow_connect)
    before = {t for t in threading.enumerate() if t.name == "kanban-bounded-call"}
    with pytest.raises(TimeoutError):
        ksc._bounded_call(ksc.Identity(url=server.url, key="k" * 16, gateway="gw"), "POST",
                          "/v2/machines/me/metrics", {"cpus": 4, **CPU}, 1.0)
    left = [t for t in threading.enumerate() if t.name == "kanban-bounded-call" and t not in before]
    for t in left:
        t.join(1.5)
    assert not any(t.is_alive() for t in left), "a connect that finished after expiry left a trickling thread alive"
