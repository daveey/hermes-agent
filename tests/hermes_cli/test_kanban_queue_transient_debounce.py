"""A single transient Board failure must not flip a card's queue reason or post a comment pair.

Live, a lone reserve or feed-pull timeout between two successful passes recorded ``board_error`` and then the
previous reason again, posting two worker-placement comments each time (31 such observations in 24 h on the hub).
Now a transient failure (transport ``OSError``, HTTP 5xx) changes the reason only on the second consecutive
transient failure of the same request kind (feed pull, or that card's own reserve) for that card; every unconfirmed failure is still recorded as a ``worker_queue_board_error``
diagnostic event with its provenance. Separately, a timed-out reserve (revision-fenced, so a same-revision replay
returns the recorded reservation) is resent once when the shared deadline still has room.

Everything Board-facing runs through a real ``ThreadingHTTPServer``, the real ``client.Hub`` HTTP path,
``reevaluate`` and ``dispatch_tick``.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_placement as placement
from hermes_cli import kanban_queue_reason as qr
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_worker_reserve_client as fixtures
from tests.hermes_cli import test_kanban_queue_reason_hold as hold

# These pin which failures are commented, not when (the hold: test_kanban_queue_reason_hold).
pytestmark = pytest.mark.usefixtures("no_comment_hold")

no_comment_hold = hold.no_comment_hold
conn = fixtures.conn
CFG = {"worker_pools": {"hub": ["worker-1"]}}
TIMEOUT_S = 0.8  # scaled-down dispatch budget (a good pass = reserve + feed pull must fit)
RESERVE = "/v2/boards/default/placement/reserve"
QUEUED = {"state": "queued", "task_version": 4,
          "observation": {"version": 1, "code": "capability_mismatch", "candidates": []}}


class _Board:
    """A real kept-alive HTTP Board. ``script`` maps a path prefix to queued actions: ``"ok"``, ``"hang"`` (no
    answer within any test budget) or ``"status:<n>"``. An empty queue answers ``"ok"``."""

    def __init__(self):
        self.script = {"GET": [], "POST": []}
        self.requests, self.bodies, self.release, self.cursor = [], [], threading.Event(), 0
        board = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _serve(self):
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b""
                board.requests.append((self.command, self.path.split("?")[0]))
                board.bodies.append(body)
                queue = board.script[self.command]
                action = queue.pop(0) if queue else "ok"
                if action == "hang":
                    board.release.wait(10)
                    self.close_connection = True
                    return
                status = int(action.split(":")[1]) if action.startswith("status:") else 200
                if self.command == "GET":
                    payload = {"changes": [], "next": board.cursor, "more": False}
                elif self.path.startswith(RESERVE):
                    payload = QUEUED
                else:
                    payload = {"path": self.path, "body": body.decode() or None}
                raw = json.dumps(payload if status == 200 else {"error": "unavailable"}).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = _serve

            def log_message(self, format, *args):  # the base signature
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
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

    def posts(self):
        return [p for m, p in self.requests if m == "POST"]


@pytest.fixture
def board(conn, monkeypatch):
    for var in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(placement, "config", lambda: CFG)
    monkeypatch.setattr(wp, "_queued", {})  # every pass asks the Board (no 60 s queued-reply throttle)
    monkeypatch.setattr(wp, "_refused", {})
    monkeypatch.setattr(wp, "DISPATCH_NETWORK_BUDGET_S", TIMEOUT_S)
    with _Board() as b:
        b.cursor = int((sync.sync_state(conn) or {}).get("cursor") or 0)
        monkeypatch.setattr(client, "load_identity", lambda: client.Identity(b.url, "synthetic", "hub"))
        yield b


def _hub(board):
    # The dispatcher's construction: per-call timeout == shared deadline, so a timeout leaves no replay budget.
    import time
    return client.Hub(client.Identity(board.url, "synthetic", "hub"), timeout=TIMEOUT_S,
                      deadline=time.monotonic() + TIMEOUT_S)


def _reserve_pass(conn, board):
    wp._queued.clear()  # a pass asks the Board again (production throttles a queued reply for 60 s)
    hub = _hub(board)
    try:
        wp.reevaluate(conn, hub, "default", "hub", cfg=CFG)
    finally:
        hub.close()


def _reasons(conn, tid):
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
                        (tid, qr.EVENT)).fetchall()
    return [json.loads(r[0])["code"] for r in rows]


def _diagnostics(conn, tid):
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
                        (tid, qr.DIAGNOSTIC_EVENT)).fetchall()
    return [json.loads(r[0]) for r in rows]


def _comments(conn, tid):
    return [c for c in kb.list_comments(conn, tid) if c.author == "worker-placement"]


def test_single_reserve_timeout_between_successes_does_not_flip_or_comment(conn, board):
    tid = fixtures.acknowledged(conn)
    _reserve_pass(conn, board)
    assert _reasons(conn, tid) == ["capability_mismatch"] and len(_comments(conn, tid)) == 1

    board.script["POST"] = ["hang"]
    _reserve_pass(conn, board)
    assert board.posts() == [RESERVE, RESERVE], "the second reserve must actually reach the Board"
    assert _reasons(conn, tid) == ["capability_mismatch"], "one transient timeout must not flip the reason"
    assert len(_comments(conn, tid)) == 1, "one transient timeout must not post a comment"
    [diag] = _diagnostics(conn, tid)  # ... but it stays visible, with its provenance
    assert diag["code"] == "board_error" and diag["pass"] == 1
    assert diag["stage"] == "reserve" and diag["transport"] == "timeout" and diag["error_class"] == "OSError"
    assert type(diag["budget_ms"]) is int and type(diag["elapsed_ms"]) is int

    _reserve_pass(conn, board)
    assert _reasons(conn, tid) == ["capability_mismatch"] and len(_comments(conn, tid)) == 1


def test_two_consecutive_reserve_timeouts_flip_once_with_one_comment_pair(conn, board):
    tid = fixtures.acknowledged(conn)
    _reserve_pass(conn, board)
    board.script["POST"] = ["hang", "hang", "hang"]
    for _ in range(3):
        _reserve_pass(conn, board)
    assert _reasons(conn, tid) == ["capability_mismatch", "board_error"], "confirmed on the 2nd pass, deduped after"
    assert len(_comments(conn, tid)) == 2
    assert [d["pass"] for d in _diagnostics(conn, tid)] == [1], "only the unconfirmed failure is a diagnostic"
    _reserve_pass(conn, board)  # recovery
    assert _reasons(conn, tid) == ["capability_mismatch", "board_error", "capability_mismatch"]
    assert len(_comments(conn, tid)) == 3  # exactly one flip + one recovery comment


def test_a_success_between_failures_resets_the_streak(conn, board):
    tid = fixtures.acknowledged(conn)
    board.script["POST"] = ["ok", "hang", "ok", "hang", "ok"]
    for _ in range(5):
        _reserve_pass(conn, board)
    assert _reasons(conn, tid) == ["capability_mismatch"] and len(_comments(conn, tid)) == 1
    assert [d["pass"] for d in _diagnostics(conn, tid)] == [1, 1]


def _tick(conn):
    wp._queued.clear()
    wp.dispatch_tick(conn)


def _cards(conn):
    return sorted([fixtures.acknowledged(conn), fixtures.acknowledged(conn)])


def test_single_pull_timeout_fanout_does_not_flip_or_comment(conn, board):
    cards = _cards(conn)
    _tick(conn)  # pull ok + reserve ok
    assert all(_reasons(conn, t) == ["capability_mismatch"] for t in cards)
    board.script["GET"] = ["hang"]
    _tick(conn)
    assert ("GET", "/v2/boards/default/changes") in board.requests
    for tid in cards:
        assert _reasons(conn, tid) == ["capability_mismatch"] and len(_comments(conn, tid)) == 1
        [diag] = _diagnostics(conn, tid)
        assert diag["stage"] == "pull" and diag["transport"] == "timeout" and diag["pass"] == 1
    _tick(conn)  # recovery: pull ok clears the pull streak
    board.script["GET"] = ["hang"]
    _tick(conn)  # a later lone pull timeout is again only pass 1
    for tid in cards:
        assert _reasons(conn, tid) == ["capability_mismatch"] and len(_comments(conn, tid)) == 1
        assert [d["pass"] for d in _diagnostics(conn, tid)] == [1, 1]


def test_two_consecutive_pull_timeouts_flip_each_card_once(conn, board):
    cards = _cards(conn)
    _tick(conn)
    board.script["GET"] = ["hang", "hang"]
    _tick(conn)
    _tick(conn)
    for tid in cards:
        assert _reasons(conn, tid) == ["capability_mismatch", "board_error"]
        assert len(_comments(conn, tid)) == 2
        assert (qr.latest(conn, tid) or {}).get("stage") == "pull"


def test_isolated_reserve_and_pull_failures_do_not_combine(conn, board, monkeypatch):
    """X's reserve times out; the next tick is healthy but the batch cursor skips X; then one lone pull times out.
    The Board answered the feed in between, and the two failures hit different requests: no flip."""
    monkeypatch.setattr(wp, "MAX_RESERVES_PER_TICK", 1)
    first, second = _cards(conn)
    _tick(conn)  # pull ok, first reserved ok
    _tick(conn)  # pull ok, second reserved ok
    board.script["POST"] = ["hang"]
    _tick(conn)  # pull ok, first's reserve times out
    _tick(conn)  # healthy: pull ok, second reserved ok (first skipped by the cursor)
    board.script["GET"] = ["hang"]
    _tick(conn)  # one lone pull timeout
    for tid in (first, second):
        assert _reasons(conn, tid) == ["capability_mismatch"] and len(_comments(conn, tid)) == 1
    assert [(d["stage"], d["pass"]) for d in _diagnostics(conn, first)] == [("reserve", 1), ("pull", 1)]


def test_sustained_reserve_outage_flips_while_the_feed_is_healthy(conn, board):
    tid = fixtures.acknowledged(conn)
    _tick(conn)
    board.script["POST"] = ["hang", "status:503"]
    _tick(conn)  # pull ok, reserve times out
    _tick(conn)  # pull ok, reserve 503: still the same outage of this card's reserve
    assert _reasons(conn, tid) == ["capability_mismatch", "board_error"]
    assert len(_comments(conn, tid)) == 2


def test_an_outage_that_alternates_its_error_still_confirms(conn, board):
    tid = fixtures.acknowledged(conn)
    _reserve_pass(conn, board)
    board.script["POST"] = ["status:502", "status:503"]
    for _ in range(2):
        _reserve_pass(conn, board)
    assert _reasons(conn, tid) == ["capability_mismatch", "board_error"], "502 then 503 is one outage"
    assert qr.latest(conn, tid)["http_status"] == 503 and len(_comments(conn, tid)) == 2
    assert [(d["http_status"], d["pass"]) for d in _diagnostics(conn, tid)] == [(502, 1)]


def test_diagnostic_event_never_stores_raw_error_text(conn):
    secret = "https://user:hunter2-SECRET@hub.invalid/api?token=SECRET"
    tid = fixtures.acknowledged(conn)
    import urllib.error
    assert not qr.record_error(conn, tid, "hub", error=urllib.error.URLError(ConnectionResetError(secret)),
                               stage="reserve", timing={"budget_ms": 5, "elapsed_ms": 7, "url": secret})
    rows = [r[0] for r in conn.execute("SELECT payload FROM task_events WHERE task_id=?", (tid,)).fetchall()]
    [diag] = _diagnostics(conn, tid)
    assert diag["transport"] == "reset" and diag["budget_ms"] == 5 and "url" not in diag
    assert rows and all("SECRET" not in blob and "hunter2" not in blob for blob in rows)


def test_http_5xx_is_debounced_but_a_board_refusal_records_at_once(conn, board):
    tid = fixtures.acknowledged(conn)
    _reserve_pass(conn, board)
    board.script["POST"] = ["status:503"]
    _reserve_pass(conn, board)
    assert _reasons(conn, tid) == ["capability_mismatch"] and len(_comments(conn, tid)) == 1
    assert _diagnostics(conn, tid)[0]["http_status"] == 503

    other = fixtures.acknowledged(conn)
    error = client.HubError(403, json.dumps({"error": "not_creator"}))
    assert qr.record_error(conn, other, "hub", error=error, stage="reserve")
    assert _reasons(conn, other) == ["board_refused"] and _diagnostics(conn, other) == []


def test_diagnostic_respects_the_ownership_fence(conn):
    tid = fixtures.acknowledged(conn, created_on="other-gateway")
    assert not qr.record_error(conn, tid, "hub", error=TimeoutError("timed out"), stage="reserve")
    assert _diagnostics(conn, tid) == [] and _reasons(conn, tid) == []


# --- reserve timeout replay (real kept-alive HTTP) ---------------------------------------------------------------


def _plain_hub(board, *, timeout, deadline=None):
    return client.Hub(client.Identity(board.url, "synthetic", "hub"), timeout=timeout, deadline=deadline)


def test_reserve_timeout_replays_once_with_identical_body(board):
    board.script["POST"] = ["hang"]
    hub = _plain_hub(board, timeout=0.3)
    try:
        reply = hub.reserve("default", task_id="t_test", expected_revision=7, candidates=["worker-1"])
    finally:
        hub.close()
    assert reply["state"] == "queued"
    assert board.posts() == [RESERVE, RESERVE]
    assert board.bodies[0] == board.bodies[1] and json.loads(board.bodies[0])["expected_revision"] == 7


def test_reserve_timeout_replay_is_bounded_to_one(board):
    board.script["POST"] = ["hang", "hang"]
    hub = _plain_hub(board, timeout=0.2)
    try:
        with pytest.raises(OSError) as info:
            hub.reserve("default", task_id="t_test", expected_revision=7, candidates=["worker-1"])
    finally:
        hub.close()
    assert qr.transport_kind(info.value) == "timeout"
    assert board.posts() == [RESERVE, RESERVE]


def test_reserve_timeout_without_remaining_budget_is_not_replayed(board):
    import time
    board.script["POST"] = ["hang"]
    hub = _plain_hub(board, timeout=0.3, deadline=time.monotonic() + 0.3)
    try:
        with pytest.raises(OSError) as info:
            hub.reserve("default", task_id="t_test", expected_revision=7, candidates=["worker-1"])
    finally:
        hub.close()
    assert qr.transport_kind(info.value) == "timeout", "the real timeout, never a budget-exhausted replay"
    assert board.posts() == [RESERVE]


def test_reserve_timeout_replays_within_a_larger_shared_deadline(board):
    import time
    board.script["POST"] = ["hang"]
    hub = _plain_hub(board, timeout=0.2, deadline=time.monotonic() + 2.0)
    try:
        assert hub.reserve("default", task_id="t_test", expected_revision=7, candidates=["worker-1"])["state"] == "queued"
    finally:
        hub.close()
    assert board.posts() == [RESERVE, RESERVE]


@pytest.mark.parametrize("call", [lambda hub: hub.leases("default", {"op": "claim"}),
                                  lambda hub: hub.ops("default", [{"op": 1}])], ids=["leases", "ops"])
def test_non_idempotent_post_timeout_is_not_replayed(board, call):
    board.script["POST"] = ["hang"]
    hub = _plain_hub(board, timeout=0.2)
    try:
        with pytest.raises(OSError):
            call(hub)
    finally:
        hub.close()
    assert len(board.posts()) == 1


def test_a_run_since_the_last_failure_ends_the_streak(conn):
    """Fail, the card is claimed and runs (e.g. a lost reserve reply the Board applied), then it is ready again:
    its next lone failure is a first pass, not a confirmation."""
    import time
    tid = fixtures.acknowledged(conn)
    assert not qr.record_error(conn, tid, "hub", error=TimeoutError("timed out"), stage="reserve")
    assert not qr.record_error(conn, tid, "hub", error=TimeoutError("timed out"), stage="pull")
    conn.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'done', ?)", (tid, int(time.time())))
    conn.commit()
    assert not qr.record_error(conn, tid, "hub", error=TimeoutError("timed out"), stage="reserve")
    assert not qr.record_error(conn, tid, "hub", error=TimeoutError("timed out"), stage="pull")
    assert _reasons(conn, tid) == [] and [d["pass"] for d in _diagnostics(conn, tid)] == [1, 1, 1, 1]


def test_a_card_retried_only_every_few_ticks_still_confirms(conn, board, monkeypatch):
    """A reserve OSError ends the batch, so with several queued cards each is retried only every few ticks, however
    far apart in wall-clock time: its second failed attempt still confirms (no time window hides the outage)."""
    import time
    from types import SimpleNamespace
    clock = [1000.0]
    monkeypatch.setattr(qr, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    cards = sorted(fixtures.acknowledged(conn) for _ in range(3))
    _tick(conn)  # all three reserved ok (one tick, batch of up to 4)
    board.script["POST"] = ["hang"] * 6
    for _ in range(6):
        clock[0] += 3600.0  # an hour between ticks
        _tick(conn)
    for tid in cards:
        assert _reasons(conn, tid) == ["capability_mismatch", "board_error"], tid
        assert len(_comments(conn, tid)) == 2


def test_a_feed_reset_ends_pull_streaks(conn):
    tid = fixtures.acknowledged(conn)
    qr.record_pull_error(conn, "hub", error=TimeoutError("timed out"))
    qr.record_pull_error(conn, "hub", error=client.Resync("reset"))
    qr.record_pull_error(conn, "hub", error=TimeoutError("timed out"))
    assert _reasons(conn, tid) == [] and [d["pass"] for d in _diagnostics(conn, tid)] == [1, 1]


def test_a_non_transient_pull_error_does_not_end_a_card_streak(conn):
    tid = fixtures.acknowledged(conn)
    assert not qr.record_error(conn, tid, "hub", error=TimeoutError("timed out"), stage="reserve")
    qr.record_error(conn, tid, "hub", error=client.HubError(401, "{}"), stage="pull")
    assert qr.record_error(conn, tid, "hub", error=TimeoutError("timed out"), stage="reserve")
