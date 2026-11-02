"""Placement failures must not monopolize a dispatcher tick or strand local work."""
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_placement as placement
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_worker_reserve_client as fixtures

conn = fixtures.conn
gws = fixtures.gws
svc = fixtures.svc
CFG = {"worker_pools": {"hub": ["worker-1"]}}


def test_transport_failure_stops_reservation_batch(conn, monkeypatch):
    for _ in range(8):
        fixtures.acknowledged(conn)
    hub = fixtures.RecordingHub()
    def fail(board, **body):
        hub.calls.append(body)
        raise OSError("transport unavailable")
    hub.reserve = fail
    monkeypatch.setattr(client, "pull", lambda *a, **kw: None)
    assert wp.reevaluate(conn, hub, "default", "hub", cfg=CFG) == 0
    assert len(hub.calls) == 1


def test_http_budget_is_shared_across_requests(monkeypatch):
    from contextlib import contextmanager
    clock, timeouts = [100.0], []
    from types import SimpleNamespace
    monkeypatch.setattr(client, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")  # the urlopen path; kept-alive: keepalive tests
    @contextmanager
    def open_response(request, *, timeout):
        from io import BytesIO
        timeouts.append(timeout)
        clock[0] += 1.0
        yield BytesIO(b"{}")
    monkeypatch.setattr(client.urllib.request, "urlopen", open_response)
    hub = client.Hub(client.Identity("http://example.invalid", "synthetic", "hub"), deadline=102.0)
    hub.changes("default", 0)
    hub.reserve("default", task_id="one", expected_revision=1, candidates=["worker-1"])
    with pytest.raises(TimeoutError, match="budget exhausted"):
        hub.changes("default", 1)
    assert timeouts == [2.0, 1.0]


def test_reservation_batch_is_bounded(conn, monkeypatch):
    for _ in range(12):
        fixtures.acknowledged(conn)
    hub = fixtures.RecordingHub()
    monkeypatch.setattr(client, "pull", lambda *a, **kw: None)
    wp.reevaluate(conn, hub, "default", "hub", cfg=CFG)
    assert 0 < len(hub.calls) <= 4


@pytest.mark.parametrize("error", [ValueError("bad JSON"), sqlite3.OperationalError("bad replica")])
def test_unexpected_placement_failure_still_spawns_local_work(gws, monkeypatch, error, caplog):
    origin, _ = gws
    origin.enter()
    local = kb.create_task(origin.conn, title="Host work", assignee="default")
    origin.round()
    def fail(db):
        raise error
    monkeypatch.setattr(wp, "dispatch_tick", fail)
    result = origin.tick(reconcile_orphans=False)
    assert local in [row[0] for row in result.spawned]
    assert "worker placement" in caplog.text


def test_hanging_reserve_times_out_and_local_work_spawns_same_tick(gws, monkeypatch):
    origin, _ = gws
    origin.enter()
    local = kb.create_task(origin.conn, title="Host work", assignee="default")
    origin.round()
    queued = [fixtures.acknowledged(origin.conn) for _ in range(8)]
    entered, release = Event(), Event()
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            entered.set()
            release.wait(10)
            self.close_connection = True
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(client, "load_identity", lambda: client.Identity(
        f"http://127.0.0.1:{server.server_port}", "synthetic", "hub"))
    monkeypatch.setattr(client, "pull", lambda *a, **kw: None)
    monkeypatch.setattr(placement, "config", lambda: CFG)
    try:
        start = time.monotonic()
        result = origin.tick(reconcile_orphans=False)
        assert time.monotonic() - start < 6
        assert entered.is_set()
        assert len(requests) == 1
        assert local in [row[0] for row in result.spawned]
        assert all(kb.get_task(origin.conn, tid).status == "ready" for tid in queued)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()
