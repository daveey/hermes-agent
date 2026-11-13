"""Board-error timing provenance for reserve/repin failures.

A live timeout recorded as ``stage=reserve, transport=timeout`` cannot tell a shared Board budget that was
already starved when the call started (``budget_ms`` small) from a slow Board that had the full budget and
still did not answer (``elapsed_ms`` large). Both fields are provenance only:

* ``budget_ms`` = max(0, round((deadline - monotonic() at call start) * 1000)), int, capped at 86_400_000;
  omitted when the hub has no deadline or it is non-finite / not a real number.
* ``elapsed_ms`` = max(0, round(call elapsed monotonic seconds * 1000)), int, capped at 86_400_000.
* Recorded on reserve/repin OSError and HubError errors; never part of event deduplication.
"""
import json
import math
import time as _time

import pytest

from hermes_cli import kanban_queue_reason as qr
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_worker_reserve_client as fixtures
from tests.hermes_cli import test_kanban_queue_reason_hold as hold


@pytest.fixture(autouse=True)
def _confirm_on_first_pass(monkeypatch):
    """These tests pin the provenance of the recorded board_error, not the transient-failure debounce
    (tests/hermes_cli/test_kanban_queue_transient_debounce.py): confirm a transient failure on its first pass."""
    from hermes_cli import kanban_queue_reason

    monkeypatch.setattr(kanban_queue_reason, "CONFIRM_PASSES", 1)


no_comment_hold = hold.no_comment_hold
conn = fixtures.conn
CFG = {"worker_pools": {"hub": ["worker-1"]}}
SECRET = "https://user:hunter2-SECRET@hub.invalid/api?token=SECRET"
CAP_MS = 86_400_000
T0 = 1000.0


class FakeClock:
    """``time`` stand-in: a controllable monotonic(); everything else is the real module."""

    def __init__(self, start=T0):
        self.now = start

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(_time, name)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    for module in (wp, client, qr):
        monkeypatch.setattr(module, "time", fake)
    monkeypatch.setattr(client, "pull", lambda *a, **kw: None)
    return fake


class DeadlineHub(fixtures.RecordingHub):
    """Hub-like double: carries an absolute monotonic ``deadline`` like ``client.Hub``; each call takes
    ``takes`` seconds of fake clock and then raises ``error``."""

    def __init__(self, clock, *, deadline, takes, error):
        super().__init__()
        self.deadline, self._clock, self._takes, self._error = deadline, clock, takes, error

    def _fail(self, board, **body):
        self.calls.append((board, body))
        self._clock.now += self._takes
        raise self._error

    reserve = _fail
    repin = _fail


def _arm(monkeypatch, action):
    if action == "repin":
        monkeypatch.setattr(wp, "requested_move", lambda conn, task_id: ["worker-1"])


def _run(conn, hub):
    assert wp.reevaluate(conn, hub, "default", "hub", cfg=CFG) == 0
    assert hub.calls, "the reserve/repin call must have been made"


def _blobs(conn, tid):
    return [r[0] for r in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                                       (tid, qr.EVENT)).fetchall()]


def _latest(conn, tid):
    value = qr.latest(conn, tid)
    assert value is not None, "an observation must have been recorded"
    return value


def _is_int(value):
    return type(value) is int


@pytest.mark.parametrize("action", ["reserve", "repin"])
def test_starved_budget_timeout_records_budget_and_elapsed(conn, monkeypatch, clock, action):
    tid = fixtures.acknowledged(conn)
    _arm(monkeypatch, action)
    hub = DeadlineHub(clock, deadline=T0 + 0.1, takes=0.1, error=TimeoutError("timed out " + SECRET))
    _run(conn, hub)
    value = _latest(conn, tid)
    assert value["stage"] == action and value["transport"] == "timeout"
    assert value.get("budget_ms") == 100 and _is_int(value.get("budget_ms"))
    assert value.get("elapsed_ms") == 100 and _is_int(value.get("elapsed_ms"))
    assert all("SECRET" not in blob for blob in _blobs(conn, tid))


@pytest.mark.parametrize("action", ["reserve", "repin"])
def test_full_budget_contrast_distinguishes_slow_board(conn, monkeypatch, clock, action):
    tid = fixtures.acknowledged(conn)
    _arm(monkeypatch, action)
    hub = DeadlineHub(clock, deadline=T0 + 2.0, takes=0.1, error=TimeoutError(SECRET))
    _run(conn, hub)
    value = _latest(conn, tid)
    assert value.get("budget_ms") == 2000 and value.get("elapsed_ms") == 100


@pytest.mark.parametrize("action", ["reserve", "repin"])
def test_hub_error_records_timing(conn, monkeypatch, clock, action):
    tid = fixtures.acknowledged(conn)
    _arm(monkeypatch, action)
    hub = DeadlineHub(clock, deadline=T0 + 2.0, takes=0.25, error=client.HubError(503, SECRET))
    _run(conn, hub)
    value = _latest(conn, tid)
    assert value["error_class"] == "HubError" and value["http_status"] == 503 and value["stage"] == action
    assert value.get("budget_ms") == 2000 and value.get("elapsed_ms") == 250


@pytest.mark.parametrize("action", ["reserve", "repin"])
def test_exhausted_deadline_clamps_budget_to_zero(conn, monkeypatch, clock, action):
    tid = fixtures.acknowledged(conn)
    _arm(monkeypatch, action)
    hub = DeadlineHub(clock, deadline=T0 - 5.0, takes=0.0, error=client.BoardBudgetExhausted("spent"))
    _run(conn, hub)
    value = _latest(conn, tid)
    assert value["transport"] == "budget_exhausted"
    assert value.get("budget_ms") == 0 and value.get("elapsed_ms") == 0


def test_budget_and_elapsed_are_capped(conn, monkeypatch, clock):
    tid = fixtures.acknowledged(conn)
    hub = DeadlineHub(clock, deadline=T0 + 10 * 86_400.0, takes=3 * 86_400.0, error=TimeoutError(SECRET))
    _run(conn, hub)
    value = _latest(conn, tid)
    assert value.get("budget_ms") == CAP_MS and value.get("elapsed_ms") == CAP_MS


@pytest.mark.parametrize("deadline", [None, math.nan, math.inf, -math.inf, "1001.0", 1001 + 0j, object(), True, False])
def test_unknown_or_invalid_deadline_omits_budget_but_keeps_elapsed(conn, monkeypatch, clock, deadline):
    tid = fixtures.acknowledged(conn)
    hub = DeadlineHub(clock, deadline=deadline, takes=0.1, error=TimeoutError(SECRET))
    _run(conn, hub)
    value = _latest(conn, tid)
    assert value["stage"] == "reserve" and value["transport"] == "timeout"
    assert "budget_ms" not in value
    assert value.get("elapsed_ms") == 100


@pytest.mark.parametrize("action", ["reserve", "repin"])
def test_existing_no_deadline_double_still_works(conn, monkeypatch, clock, action):
    """Existing doubles have no ``deadline`` attribute at all: no crash, no budget, elapsed still measured."""
    tid = fixtures.acknowledged(conn)
    _arm(monkeypatch, action)
    hub = fixtures.RecordingHub()
    assert not hasattr(hub, "deadline")

    def fail(board, **body):
        clock.now += 0.04
        raise ConnectionRefusedError(111, SECRET)
    setattr(hub, action, fail)
    assert wp.reevaluate(conn, hub, "default", "hub", cfg=CFG) == 0
    value = _latest(conn, tid)
    assert value["stage"] == action and value["transport"] == "refused"
    assert "budget_ms" not in value and value.get("elapsed_ms") == 40


def test_successful_reserve_reply_carries_no_timing(conn, monkeypatch, clock):
    tid = fixtures.acknowledged(conn)
    hub = fixtures.RecordingHub()
    hub.deadline = T0 + 2.0
    assert wp.reevaluate(conn, hub, "default", "hub", cfg=CFG) == 1
    value = _latest(conn, tid)
    assert "budget_ms" not in value and "elapsed_ms" not in value


def test_timing_fields_do_not_change_deduplication(conn, monkeypatch, clock):
    tid = fixtures.acknowledged(conn)
    _run(conn, DeadlineHub(clock, deadline=clock.now + 0.1, takes=0.1, error=TimeoutError(SECRET)))
    before = _blobs(conn, tid)
    assert len(before) == 1 and json.loads(before[0]).get("budget_ms") == 100
    _run(conn, DeadlineHub(clock, deadline=clock.now + 2.0, takes=1.5, error=TimeoutError(SECRET)))
    assert _blobs(conn, tid) == before


@pytest.mark.parametrize("timing", [
    {"budget_ms": True, "elapsed_ms": False}, {"budget_ms": -1, "elapsed_ms": CAP_MS + 1},
    {"budget_ms": 1.5, "elapsed_ms": "100"}, {"budget_ms": None, "elapsed_ms": [100]}, "budget_ms=100",
])
def test_record_error_drops_out_of_range_or_non_int_timing(conn, timing):
    tid = fixtures.acknowledged(conn)
    assert qr.record_error(conn, tid, "hub", error=TimeoutError(SECRET), stage="reserve", timing=timing)
    value = _latest(conn, tid)
    assert "budget_ms" not in value and "elapsed_ms" not in value


def test_record_error_keeps_only_allowlisted_timing_keys(conn):
    tid = fixtures.acknowledged(conn)
    timing = {"budget_ms": 0, "elapsed_ms": CAP_MS, "deadline": 5, "url": SECRET}
    assert qr.record_error(conn, tid, "hub", error=TimeoutError(SECRET), stage="reserve", timing=timing)
    value = _latest(conn, tid)
    assert value["budget_ms"] == 0 and value["elapsed_ms"] == CAP_MS
    assert "deadline" not in value and "url" not in value
    assert all("SECRET" not in blob for blob in _blobs(conn, tid))


@pytest.mark.parametrize("error", [client.HubError(503, SECRET), TimeoutError(SECRET)])
def test_timing_is_measured_from_the_failing_call_not_the_tick(conn, monkeypatch, clock, error):
    """An earlier card's successful reserve in the same tick spends time; the next card's failure reports only the
    budget left when ITS call started and how long ITS call took."""
    first, second = sorted([fixtures.acknowledged(conn), fixtures.acknowledged(conn)])
    hub = fixtures.RecordingHub()
    hub.deadline = T0 + 2.0
    queued = hub.reserve

    def reserve(board, **body):
        if body["task_id"] == first:
            clock.now += 1.0
            return queued(board, **body)
        hub.calls.append((board, body))
        clock.now += 0.25
        raise error
    hub.reserve = reserve
    wp.reevaluate(conn, hub, "default", "hub", cfg=CFG)
    assert [body["task_id"] for _, body in hub.calls] == [first, second]
    value = _latest(conn, second)
    assert value["code"] == "board_error"
    assert value.get("budget_ms") == 1000 and value.get("elapsed_ms") == 250


@pytest.mark.usefixtures("no_comment_hold")
def test_real_http_reserve_after_slow_local_pull_records_starved_budget_timeout(conn, monkeypatch):
    """End to end through ``dispatch_tick``: a slow local pull spends most of the shared 2 s tick deadline, so the
    real reserve POST reaches a Board that never answers with only the remainder and times out. The record must show
    that small starting budget (not the full 2000 ms) and an elapsed time bounded by it, and the timeout must still be
    recorded on a card the Board had already assigned (never hidden behind that earlier assignment)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_placement as placement

    tid = fixtures.acknowledged(conn, runs_on='["machine:worker-1"]')
    assigned = {"state": "assigned", "machine": "worker-1", "reservation_id": "r1", "expires_at": None,
                "observation": {"version": 1, "code": "reserved", "candidates": []}}
    assert qr.record_reply(conn, tid, "hub", assigned, ["worker-1"])
    before = _latest(conn, tid)
    assert before["state"] == "assigned" and before["current"] is True
    comments = len(kb.list_comments(conn, tid))

    pull_delay_s = 1.5
    requests, release = [], threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            release.wait(10)  # never answers within the tick budget
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(client, "load_identity", lambda: client.Identity(
        f"http://127.0.0.1:{server.server_port}", "synthetic", "hub"))
    # health: allow HX013 -- elapsed local apply work consuming the shared deadline is the tested behavior
    monkeypatch.setattr(client, "pull", lambda *a, **kw: _time.sleep(pull_delay_s))
    monkeypatch.setattr(placement, "config", lambda: CFG)
    try:
        start = _time.monotonic()
        wp.dispatch_tick(conn)
        took = _time.monotonic() - start
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()

    assert requests == ["/v2/boards/default/placement/reserve"], "the reserve must actually reach the Board"
    assert took < wp.DISPATCH_NETWORK_BUDGET_S + 1.0
    value = _latest(conn, tid)
    assert value["code"] == "board_error" and value["current"] is True
    assert value["stage"] == "reserve" and value["transport"] == "timeout" and value["error_class"] == "OSError"
    budget_cap_ms = round((wp.DISPATCH_NETWORK_BUDGET_S - pull_delay_s) * 1000)
    assert _is_int(value["budget_ms"]) and 0 < value["budget_ms"] <= budget_cap_ms
    assert _is_int(value["elapsed_ms"]) and value["budget_ms"] - 50 <= value["elapsed_ms"] <= value["budget_ms"] + 500
    assert len(kb.list_comments(conn, tid)) == comments + 1
    assert kb.get_task(conn, tid).status == "ready"


class _SubtractionFails(float):
    """A finite float whose arithmetic blows up: stands in for any diagnostic calculation that raises."""

    def __sub__(self, other):
        raise OverflowError("diagnostic arithmetic failed")


@pytest.mark.parametrize("deadline", [10**400, -(10**400)], ids=["+10**400", "-10**400"])
@pytest.mark.parametrize("action", [("reserve", TimeoutError(SECRET)), ("repin", client.HubError(503, SECRET))])
def test_huge_int_deadline_is_unusable_and_never_masks_the_error(conn, monkeypatch, clock, deadline, action):
    """An int too large for a float is no usable deadline: budget omitted, elapsed kept, original error recorded."""
    action, error = action
    tid = fixtures.acknowledged(conn)
    _arm(monkeypatch, action)
    hub = DeadlineHub(clock, deadline=deadline, takes=0.1, error=error)
    _run(conn, hub)
    value = _latest(conn, tid)
    assert value["code"] == "board_error" and value["stage"] == action
    assert "budget_ms" not in value and value.get("elapsed_ms") == 100


@pytest.mark.parametrize("deadline,budget_ms", [(1e306, CAP_MS), (-1e306, 0), (10**300, CAP_MS), (-(10**300), 0)])
@pytest.mark.parametrize("action", ["reserve", "repin"])
def test_extreme_finite_deadline_clamps_before_scaling(conn, monkeypatch, clock, deadline, budget_ms, action):
    tid = fixtures.acknowledged(conn)
    _arm(monkeypatch, action)
    hub = DeadlineHub(clock, deadline=deadline, takes=0.1, error=TimeoutError(SECRET))
    _run(conn, hub)
    value = _latest(conn, tid)
    assert value["stage"] == action and value["transport"] == "timeout"
    assert value.get("budget_ms") == budget_ms and _is_int(value["budget_ms"]) and value.get("elapsed_ms") == 100


def test_extreme_elapsed_is_capped_not_overflowed(conn, monkeypatch, clock):
    tid = fixtures.acknowledged(conn)
    hub = DeadlineHub(clock, deadline=T0 + 2.0, takes=1e306, error=TimeoutError(SECRET))
    _run(conn, hub)
    value = _latest(conn, tid)
    assert value["transport"] == "timeout" and value.get("budget_ms") == 2000 and value.get("elapsed_ms") == CAP_MS


@pytest.mark.parametrize("seconds,expected", [
    (10**400, None), (-(10**400), None), (1e306, CAP_MS), (-1e306, 0), (10**300, CAP_MS), (1.7976931348623157e308, CAP_MS),
    (86_400, CAP_MS), (0.1, 100), (5, 5000),
], ids=["+10**400", "-10**400", "+1e306", "-1e306", "10**300", "float_max", "one_day", "0.1s", "5s_int"])
def test_ms_handles_extremes_without_overflow(seconds, expected):
    ms = qr._ms(seconds)
    assert ms == expected and (ms is None or _is_int(ms))


def test_budget_calculation_failure_omits_budget_and_still_calls(conn, monkeypatch, clock):
    tid = fixtures.acknowledged(conn)
    hub = DeadlineHub(clock, deadline=_SubtractionFails(T0 + 2.0), takes=0.1, error=TimeoutError(SECRET))
    _run(conn, hub)
    value = _latest(conn, tid)
    assert value["stage"] == "reserve" and value["transport"] == "timeout"
    assert "budget_ms" not in value and value.get("elapsed_ms") == 100


@pytest.mark.parametrize("action", [("reserve", TimeoutError(SECRET)), ("repin", client.HubError(503, SECRET))])
def test_timing_calculation_failure_never_masks_the_board_error(conn, monkeypatch, clock, action):
    action, error = action
    tid = fixtures.acknowledged(conn)
    _arm(monkeypatch, action)

    def broken(seconds):
        raise OverflowError("diagnostic arithmetic failed")
    monkeypatch.setattr(qr, "_ms", broken)
    hub = DeadlineHub(clock, deadline=T0 + 2.0, takes=0.1, error=error)
    _run(conn, hub)
    value = _latest(conn, tid)
    assert value["code"] == "board_error" and value["stage"] == action
    assert "budget_ms" not in value and "elapsed_ms" not in value
