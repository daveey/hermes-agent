"""Per-attempt timing provenance for the initial feed pull in ``dispatch_tick``.

A live pull timeout fanned out to every origin card as ``stage=pull, transport=timeout`` with no timing, so a
first page that had the whole shared tick budget and got no answer looked the same as a second page that started
with only the remainder left after page 1's local apply. The pull's ``board_error`` events carry the same
allowlisted fields as reserve/repin (``budget_ms`` / ``elapsed_ms``), and they describe the FAILED HTTP ATTEMPT:

* ``budget_ms`` = budget left on the shared deadline when that GET started (not when the outer pull started);
* ``elapsed_ms`` = how long that GET took to fail (not the outer pull's total time).

Diagnostics only: every origin card still gets its own board_error and comment; no suppression, retry or timeout
change. Everything runs through a real ``ThreadingHTTPServer``, real ``client.Hub`` HTTP and real
``client.pull``; only identity, the tick budget (scaled down from 2 s) and a local apply delay are adapted.
"""
import json
import threading
import time as _time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_placement as placement
from hermes_cli import kanban_queue_reason as qr
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_worker_reserve_client as fixtures

conn = fixtures.conn
CFG = {"worker_pools": {"hub": ["worker-1"]}}
BUDGET_S = 1.0  # the production 2 s shared tick deadline, scaled down for a quick test
APPLY_DELAY_S = 0.6  # page 1's local apply spends most of it
FEED = "/v2/boards/default/changes"


def _is_int(value):
    return type(value) is int


def _pull_events(conn, tid):
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, qr.EVENT)).fetchall()
    return [v for v in (json.loads(r[0]) for r in rows) if isinstance(v, dict) and v.get("stage") == "pull"]


class _Board:
    """A real HTTP Board feed. ``pages`` are answered in order; ``None`` = hold the GET past the budget."""

    def __init__(self, pages):
        self.pages, self.requests, self.release = list(pages), [], threading.Event()
        board = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # keep-alive: page 2 may reuse page 1's socket

            def do_GET(self):
                board.requests.append(self.path)
                page = board.pages.pop(0) if board.pages else None
                if page is None:
                    board.release.wait(10)  # never answers within the tick budget
                    self.close_connection = True
                    return
                raw = json.dumps(page).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):  # reserve must not be reached after a failed pull
                board.requests.append(self.path)
                self.send_response(500)
                self.send_header("content-length", "0")
                self.end_headers()

            def log_message(self, format, *args):  # the base signature
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"


def _arm(monkeypatch, board):
    for var in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(client, "load_identity", lambda: client.Identity(board.url, "synthetic", "hub"))
    monkeypatch.setattr(placement, "config", lambda: CFG)
    monkeypatch.setattr(wp, "DISPATCH_NETWORK_BUDGET_S", BUDGET_S)


def _two_origin_cards(conn):
    cards = sorted([fixtures.acknowledged(conn), fixtures.acknowledged(conn)])
    return cards, {tid: len(kb.list_comments(conn, tid)) for tid in cards}


def _tick(conn):
    start = _time.monotonic()
    wp.dispatch_tick(conn)
    return _time.monotonic() - start


def _assert_fanout_unsuppressed(conn, cards, comments):
    """Every origin card gets its own pull board_error and comment (provenance never suppresses the fanout)."""
    values = {}
    for tid in cards:
        events = _pull_events(conn, tid)
        assert len(events) == 1, f"{tid}: exactly one pull board_error expected, got {events}"
        value = qr.latest(conn, tid)
        assert value is not None, f"{tid}: no queue observation recorded"
        assert value["code"] == "board_error" and value["current"] is True
        assert value["stage"] == "pull" and value["transport"] == "timeout" and value["error_class"] == "OSError"
        assert len(kb.list_comments(conn, tid)) == comments[tid] + 1, f"{tid}: board_error comment missing"
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "ready"
        values[tid] = value
    return values


def test_first_page_timeout_records_the_full_attempt_budget(conn, monkeypatch):
    """Page 1 gets the whole shared budget and no answer: the failed GET's budget is ~the full tick budget and its
    elapsed time ~that budget (a slow Board, not a starved call)."""
    cards, comments = _two_origin_cards(conn)
    with _Board([None]) as board:
        _arm(monkeypatch, board)
        took = _tick(conn)
    assert [p.split("?")[0] for p in board.requests] == [FEED], "the first feed GET must actually reach the Board"
    assert took < BUDGET_S + 1.0
    values = _assert_fanout_unsuppressed(conn, cards, comments)
    full_ms = round(BUDGET_S * 1000)
    for tid, value in values.items():
        assert "budget_ms" in value, f"{tid}: failed pull attempt carries no budget_ms: {value}"
        assert "elapsed_ms" in value, f"{tid}: failed pull attempt carries no elapsed_ms: {value}"
        assert _is_int(value["budget_ms"]) and full_ms - 150 <= value["budget_ms"] <= full_ms
        assert _is_int(value["elapsed_ms"]) and value["budget_ms"] - 50 <= value["elapsed_ms"] <= value["budget_ms"] + 300


def test_second_page_timeout_records_only_the_budget_left_after_page_one_apply(conn, monkeypatch):
    """Page 1 answers ``more`` and its local apply spends most of the shared deadline; page 2's GET then gets no
    answer. The record describes THAT attempt: its starting budget is only the remainder (never the full initial
    budget the outer pull started with) and its elapsed time is bounded by that remainder (not the whole pull)."""
    cards, comments = _two_origin_cards(conn)
    cursor = int((sync.sync_state(conn) or {}).get("cursor") or 0)
    apply_remote_changes, applied = sync.apply_remote_changes, []

    def slow_apply(*args, **kwargs):
        applied.append(kwargs.get("new_cursor"))
        # health: allow HX013 -- local apply work consuming the shared deadline is the tested behavior
        _time.sleep(APPLY_DELAY_S)
        return apply_remote_changes(*args, **kwargs)

    monkeypatch.setattr(sync, "apply_remote_changes", slow_apply)
    with _Board([{"changes": [], "next": cursor + 1, "more": True}, None]) as board:
        _arm(monkeypatch, board)
        took = _tick(conn)
    assert [p.split("?")[0] for p in board.requests] == [FEED, FEED], "page 2 must actually reach the Board"
    assert f"since={cursor + 1}" in board.requests[1], "page 2 must follow page 1's cursor"
    assert applied == [cursor + 1], "page 1 must have been applied locally before page 2"
    assert took < BUDGET_S + 1.0
    values = _assert_fanout_unsuppressed(conn, cards, comments)
    remainder_cap_ms = round((BUDGET_S - APPLY_DELAY_S) * 1000)
    for tid, value in values.items():
        assert "budget_ms" in value, f"{tid}: failed page-2 attempt carries no budget_ms: {value}"
        assert "elapsed_ms" in value, f"{tid}: failed page-2 attempt carries no elapsed_ms: {value}"
        assert _is_int(value["budget_ms"]) and 0 < value["budget_ms"] <= remainder_cap_ms, (
            f"{tid}: page 2 claimed {value['budget_ms']} ms; only <= {remainder_cap_ms} ms was left")
        assert _is_int(value["elapsed_ms"]) and value["budget_ms"] - 50 <= value["elapsed_ms"] <= value["budget_ms"] + 300
        assert value["elapsed_ms"] < round(APPLY_DELAY_S * 1000), "elapsed must be the attempt's, not the outer pull's"
