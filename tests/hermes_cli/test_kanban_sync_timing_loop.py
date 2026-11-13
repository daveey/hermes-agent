"""The real sync loop, real Reporter and real feed socket under bounded timing diagnostics.

A controlled local Board stand-in (HTTP for the reporter's POST, a WebSocket feed for change frames) delays the
metrics POST and sends one decoded change frame while that POST is in flight. The capture must show: the full
reporter cost (collect, the real ~1 s CPU sample and the complete private bounded transport), the decoded change
inside the send span, and the change matched to its own board's next actual service, after the report returned.
An off cycle before and after the capture writes nothing. This validates the instrumentation only; it is not a
live acceptance measurement and it subtracts no baseline.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_timing as timing
from hermes_cli import kanban_sync_timing_report as report
from hermes_cli import machine_metrics as mm
from tests.waiting import wait_until

pytest.importorskip("websockets")

POST_DELAY_S = 0.4


def _config(root: Path, until: float | None) -> None:
    lines = ["kanban:", "  admission:", "    cpu_busy_below_pct: 50"]
    if until is not None:
        lines += ["  sync:", "    diagnostics:", f"      timing_until: {until}"]
    (root / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _records(path: Path) -> list[dict]:
    """The complete records so far. The loop appends while the test polls, so a read can end inside a record."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text[:text.rfind("\n") + 1].splitlines()]


@pytest.mark.platforms("linux")  # the reporter's real CPU busy sample reads /proc/stat
def test_real_loop_records_full_report_cost_and_matches_a_change_to_its_boards_service(tmp_path, monkeypatch):
    from websockets.sync.server import serve
    import hermes_constants

    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda *a, **k: root)
    # A test process has no hermes_bootstrap boot fingerprint; stand one in so the capture's provenance is verified.
    monkeypatch.setattr(timing, "_code_identity", lambda: ("0123456789abcdef0123456789abcdef01234567", "boot", False))
    monkeypatch.setattr(timing, "_DIAG", timing.TimingDiag())
    monkeypatch.setattr(mm, "REPORT_EVERY_S", 3.0)
    # The loop's own report path is under test: no anchored sender thread, so the loop reports as on a host
    # without one (machine_metrics_sender; its delegated record is pinned in test_machine_metrics_sender).
    from hermes_cli import machine_metrics_sender
    monkeypatch.setattr(machine_metrics_sender.MetricsSender, "start", lambda self: None)
    _config(root, None)
    log = timing.log_path(root)

    stop = threading.Event()
    in_send = threading.Event()
    frame_sent = threading.Event()
    posts: list[dict] = []
    delay = threading.Event()  # never set: its timed wait is the stand-in's controlled transport delay

    class Board(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            if self.path == "/v2/machines/me/metrics":
                posts.append(body)
                if timing._DIAG.capture is not None and not in_send.is_set():
                    in_send.set()
                    frame_sent.wait(5)
                delay.wait(POST_DELAY_S)
            data = b"{}"
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def feed(ws):
        board = ws.request.path.split("/")[3]
        if board == "alpha" and in_send.wait(30):
            ws.send('{"type": "changes", "head": 7}')
            frame_sent.set()
        while not stop.is_set():
            try:
                ws.recv(timeout=0.2)
            except TimeoutError:
                continue
            except Exception:  # the client closed: this stand-in handler just ends
                return

    services: list[tuple[str, int]] = []

    def sync_board(slug, path, hub, gateway):
        services.append((slug, time.monotonic_ns()))
        return client.RoundResult(board=slug, mode="primary")

    http = ThreadingHTTPServer(("127.0.0.1", 0), Board)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    with serve(feed, "127.0.0.1", 0) as ws_server:
        threading.Thread(target=ws_server.serve_forever, daemon=True).start()
        ws_port = ws_server.socket.getsockname()[1]
        boards = [("alpha", tmp_path / "alpha.db"), ("beta", tmp_path / "beta.db")]
        monkeypatch.setattr(client, "load_identity",
                            lambda: client.Identity(f"http://127.0.0.1:{http.server_address[1]}", "k", "gw"))
        monkeypatch.setattr(client, "board_paths", lambda: boards)
        monkeypatch.setattr(client.sync, "set_lease_client", lambda _c: None)
        monkeypatch.setattr(client, "publish_pickup_if_changed", lambda _h: None)
        monkeypatch.setattr(client, "reconcile_links", lambda *a, **k: None)
        monkeypatch.setattr(client, "outbox_pending", lambda _paths: False)
        monkeypatch.setattr(client, "sync_board", sync_board)
        monkeypatch.setattr(client.FeedWatcher, "url", lambda self: f"ws://127.0.0.1:{ws_port}/v2/boards/{self.board}/ws")
        loop = threading.Thread(target=client.run_forever, args=(lambda: not stop.is_set(),),
                                kwargs={"interval": 0.3}, daemon=True)
        loop.start()
        try:
            # Off cycle: a real report completes and nothing is written.
            wait_until(lambda: len(posts) >= 1, timeout=20, what="the first (off) metrics report")
            assert not log.exists()
            _config(root, time.time() + 60)
            wait_until(lambda: any(r["ev"] == "svc" and "c_first" in r for r in _records(log)), timeout=40,
                       what="the decoded change matched to a board service")
            _config(root, None)
            wait_until(lambda: _records(log)[-1]["ev"] == "stop", timeout=20, what="the capture stop marker")
            frozen = log.read_bytes()
            seen = len(posts)
            wait_until(lambda: len(posts) > seen, timeout=20, what="a quiet report after the capture stopped")
        finally:
            stop.set()
            loop.join(15)
            http.shutdown()
            ws_server.shutdown()
    assert not loop.is_alive()
    assert log.read_bytes() == frozen  # off again: the later loop and report wrote nothing

    recs = _records(log)
    start, end = recs[0], recs[-1]
    assert start["ev"] == "start" and start["pid"] > 0
    assert (end["reason"], end["valid"], end["lost"], end["errors"]) == ("disabled", True, 0, 0)
    assert [r["seq"] for r in recs] == list(range(len(recs)))
    chg = [r for r in recs if r["ev"] == "chg"]
    assert len(chg) == 1, "only the decoded frame counts; the connect-time catch-up call is not a notification"
    change = chg[0]
    rep = next(r for r in recs if r["ev"] == "rep" and "send_t0" in r)
    assert rep["send_t0"] <= change["t"] <= rep["send_t1"], "the frame arrived while the POST was in flight"
    assert 'limit_t0' in rep  # the gated report reads the root limit
    for r in recs:  # the CPU hand-off never holds the loop: the sampler thread measured the window already
        if r['ev'] == 'rep' and 'cpu_t0' in r:
            assert 0 <= r['cpu_t1'] - r['cpu_t0'] < 100_000_000
        if r['ev'] == 'rep' and 'cpu_win_ms' in r:
            assert 1000 <= r['cpu_win_ms'] <= 2000  # observation window, not blocked loop time
    assert rep["send_t1"] - rep["send_t0"] >= int(POST_DELAY_S * 1e9)
    assert rep["send_out"] == "ok" and rep["res"] == "sent"
    assert rep["t1"] - rep["t0"] >= rep["send_t1"] - rep["send_t0"]
    svc = next(r for r in recs if r["ev"] == "svc" and r.get("c_first") == change["seq"])
    assert svc["b"] == change["b"] and svc["out"] == "ok" and svc["loop"] is True
    assert svc["t0"] >= rep["t1"], "the loop serviced the board only after the report returned"
    assert any(slug == "alpha" and rep["t1"] <= t for slug, t in services)
    summary = report.summarize(report.captures(recs)[0])
    assert summary["valid"] is True, summary["invalid"]
    served = summary["served"]
    assert len(served) == 1 and served[0]["seq"] == change["seq"]
    assert served[0]["report_overlap_ns"] == rep["t1"] - change["t"]
    assert served[0]["latency_ns"] == svc["t1"] - change["t"] >= served[0]["report_overlap_ns"]
    assert summary["coverage"] == "overlap"
    assert summary["reports"]["due"]["n"] >= 1
    assert summary["overhead"]["flushes"] >= 1


def test_off_loop_creates_no_diagnostic_file_or_thread(tmp_path, monkeypatch):
    import hermes_constants
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda *a, **k: root)
    monkeypatch.setattr(timing, "_DIAG", timing.TimingDiag())
    _config(root, None)
    rounds = []
    names_before = {t.name for t in threading.enumerate()}
    monkeypatch.setattr(client, "load_identity", lambda: client.Identity("https://example.invalid", "k", "gw"))
    monkeypatch.setattr(client, "board_paths", list)
    monkeypatch.setattr(client.sync, "set_lease_client", lambda _c: None)
    monkeypatch.setattr(client, "publish_pickup_if_changed", lambda _h: None)
    monkeypatch.setattr(client, "reconcile_links", lambda *a, **k: None)
    reports = []
    monkeypatch.setattr(mm, "Reporter", lambda _send: type("R", (), {"report_due": lambda self: reports.append(1), "pending_wake_in": lambda self: None})())

    def wait(*_a):
        rounds.append(1)
        return "period"
    monkeypatch.setattr(client, "wait_for_work", wait)
    client.run_forever(lambda: len(rounds) < 3, interval=0.1)
    assert len(reports) == 3
    assert not (root / "logs").exists()
    assert {t.name for t in threading.enumerate()} - names_before == set()
