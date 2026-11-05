"""Edges of the per-HTTP-attempt timing stamp behind pull ``board_error`` provenance.

``Hub`` stamps a failed attempt's raw ``(budget_s, elapsed_s)`` on the exception it raises (``kanban_sync_client_attempt.ATTEMPT_ATTR``);
``record_pull_error`` reads it through ``attempt_timing`` with the same bounded rounding/clamping as reserve/repin.
These tests pin: a stale-socket GET retry describes only the retry; a lost POST answer is still never replayed;
no attempt (budget exhausted before the retry) means no timing; absent or malformed stamps are omitted and never
mask the error; timing never makes a new event.
"""
import json
import math
import socket
import urllib.error
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_queue_reason as qr
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_client_attempt as attempt
from tests.hermes_cli import test_kanban_sync_hub_keepalive as keepalive
from tests.hermes_cli import test_kanban_worker_reserve_client as fixtures


@pytest.fixture(autouse=True)
def _confirm_on_first_pass(monkeypatch):
    """These tests pin the provenance of the recorded board_error, not the transient-failure debounce
    (tests/hermes_cli/test_kanban_queue_transient_debounce.py): confirm a transient failure on its first pass."""
    from hermes_cli import kanban_queue_reason

    monkeypatch.setattr(kanban_queue_reason, "CONFIRM_PASSES", 1)


conn = fixtures.conn
server = keepalive.server
CAP_MS = 86_400_000
T0 = 100.0


def _deadline_hub(srv, budget_s=2.0):
    hub = keepalive._hub(srv)
    hub.deadline = T0 + budget_s
    return hub


def _latest(conn, tid) -> dict:
    value = qr.latest(conn, tid)
    assert isinstance(value, dict), f"{tid}: no queue observation recorded"
    return value


def _pull_values(conn, tid):
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, qr.EVENT)).fetchall()
    return [v for v in (json.loads(r[0]) for r in rows) if isinstance(v, dict) and v.get("stage") == "pull"]


def _handlers(monkeypatch, method, steps, now):
    """Each request runs the next step ``(advance_s, action)``; action 'drop' loses the answer, 'status:N' replies."""
    original, queue = getattr(keepalive._Handler, method), list(steps)

    def serve(handler):
        if not queue:
            return original(handler)
        advance, action = queue.pop(0)
        n = int(handler.headers.get("content-length") or 0)
        if n:
            handler.rfile.read(n)
        with handler.server.lock:
            handler.server.requests.append((handler.command, handler.path))
        now[0] += advance  # the attempt spends this much of the client's (fake) clock
        if action == "drop":
            handler.close_connection = True
            handler.connection.shutdown(socket.SHUT_RDWR)
            return
        handler.send_response(int(action.split(":")[1]))
        handler.send_header("content-length", "0")
        handler.end_headers()

    monkeypatch.setattr(keepalive._Handler, method, serve)


@pytest.fixture
def boxed_clock(monkeypatch):
    now = [T0]
    monkeypatch.setattr(ksc, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.mark.parametrize("retry", ["drop", "status:503"])
def test_stale_get_retry_failure_is_timed_from_the_retry_only(conn, server, boxed_clock, monkeypatch, retry):
    """The reused socket loses page 1's answer after 0.4 s; the GET retry on a fresh socket then fails after
    0.25 s. The stamp is the retry's: budget = what was left when it started, elapsed = its own duration."""
    tid = fixtures.acknowledged(conn)
    hub = _deadline_hub(server)
    hub.machines()  # warm: the next GET reuses this socket
    _handlers(monkeypatch, "do_GET", [(0.4, "drop"), (0.25, retry)], boxed_clock)
    try:
        with pytest.raises(OSError if retry == "drop" else ksc.HubError) as caught:
            hub.changes("default", since=0)
    finally:
        hub.close()
    exc = caught.value
    if retry == "drop":
        assert isinstance(exc, urllib.error.URLError)  # the urlopen error contract is unchanged
    assert [m for m, _ in server.requests].count("GET") == 3 and server.connections == 2
    budget_s, elapsed_s = getattr(exc, attempt.ATTEMPT_ATTR)
    assert budget_s == pytest.approx(1.6) and elapsed_s == pytest.approx(0.25)
    qr.record_pull_error(conn, "hub", error=exc)
    value = _latest(conn, tid)
    assert value["code"] == "board_error" and value["stage"] == "pull"
    assert (value["budget_ms"], value["elapsed_ms"]) == (1600, 250)


def test_lost_post_answer_is_never_replayed_and_stamps_its_one_attempt(server, boxed_clock, monkeypatch):
    hub = _deadline_hub(server)
    hub.machines()
    _handlers(monkeypatch, "do_POST", [(0.3, "drop")], boxed_clock)
    try:
        with pytest.raises(urllib.error.URLError) as caught:
            hub.ops("default", [{"op": 1}])
    finally:
        hub.close()
    assert [m for m, _ in server.requests].count("POST") == 1
    budget_s, elapsed_s = getattr(caught.value, attempt.ATTEMPT_ATTR)
    assert budget_s == pytest.approx(2.0) and elapsed_s == pytest.approx(0.3)


def test_budget_exhausted_before_the_retry_records_no_timing(conn, server, boxed_clock, monkeypatch):
    """No retry attempt was made, so there is no attempt timing to report (unknown is omitted, not guessed)."""
    tid = fixtures.acknowledged(conn)
    hub = _deadline_hub(server)
    hub.machines()
    _handlers(monkeypatch, "do_GET", [(2.1, "drop")], boxed_clock)
    try:
        with pytest.raises(ksc.BoardBudgetExhausted) as caught:
            hub.changes("default", since=0)
    finally:
        hub.close()
    assert getattr(caught.value, attempt.ATTEMPT_ATTR, None) is None
    qr.record_pull_error(conn, "hub", error=caught.value)
    value = _latest(conn, tid)
    assert value["transport"] == "budget_exhausted"
    assert not set(qr.TIMING_KEYS) & set(value)


def test_no_deadline_stamps_elapsed_only(conn, server, boxed_clock, monkeypatch):
    tid = fixtures.acknowledged(conn)
    hub = keepalive._hub(server)  # no shared deadline: budget unknown
    _handlers(monkeypatch, "do_GET", [(0.125, "status:503")], boxed_clock)
    try:
        with pytest.raises(ksc.HubError) as caught:
            hub.changes("default", since=0)
    finally:
        hub.close()
    assert getattr(caught.value, attempt.ATTEMPT_ATTR)[0] is None
    qr.record_pull_error(conn, "hub", error=caught.value)
    value = _latest(conn, tid)
    assert "budget_ms" not in value and value["elapsed_ms"] == 125 and value["http_status"] == 503


def test_urlopen_path_stamps_its_attempt(server, boxed_clock, monkeypatch):
    monkeypatch.setattr(ksc.Hub, "_proxied", staticmethod(lambda parts: True))
    hub = _deadline_hub(server)
    _handlers(monkeypatch, "do_GET", [(0.5, "status:503")], boxed_clock)
    with pytest.raises(ksc.HubError) as caught:
        hub.changes("default", since=0)
    budget_s, elapsed_s = getattr(caught.value, attempt.ATTEMPT_ATTR)
    assert budget_s == pytest.approx(2.0) and elapsed_s == pytest.approx(0.5)


def test_absent_stamp_keeps_existing_pull_records_unchanged(conn):
    tid = fixtures.acknowledged(conn)
    qr.record_pull_error(conn, "hub", error=TimeoutError("timed out"))
    value = _latest(conn, tid)
    assert value["transport"] == "timeout" and not set(qr.TIMING_KEYS) & set(value)
    tid2 = fixtures.acknowledged(conn)
    qr.record_pull_error(conn, "hub", status=503)
    value = _latest(conn, tid2)
    assert value["http_status"] == 503 and not set(qr.TIMING_KEYS) & set(value)


class _TupleSub(tuple):
    pass


@pytest.mark.parametrize("stamp, expected", [
    ((math.nan, math.inf), {}),
    ((-math.inf, math.nan), {}),
    (("1.0", None), {}),
    ((True, False), {}),
    ((10**400, 1j), {}),
    ([1.0, 2.0], {}),
    ((1.0,), {}),
    ((1.0, 2.0, 3.0), {}),
    ("1.0,2.0", {}),
    (_TupleSub((1.0, 2.0)), {}),
    ((math.nan, 0.25), {"elapsed_ms": 250}),
    ((-5.0, 1e306), {"budget_ms": 0, "elapsed_ms": CAP_MS}),
    ((0.0004, 0.0006), {"budget_ms": 0, "elapsed_ms": 1}),
])
def test_malformed_stamps_are_omitted_and_never_mask_the_error(conn, stamp, expected):
    tid = fixtures.acknowledged(conn)
    error = urllib.error.URLError(TimeoutError("timed out"))
    setattr(error, attempt.ATTEMPT_ATTR, stamp)
    assert qr.attempt_timing(error) == expected
    qr.record_pull_error(conn, "hub", error=error)
    value = _latest(conn, tid)
    assert value["code"] == "board_error" and value["transport"] == "timeout" and value["error_class"] == "OSError"
    assert {k: value[k] for k in qr.TIMING_KEYS if k in value} == expected


def test_a_raising_stamp_attribute_still_records_the_error(conn):
    class Hostile(OSError):
        @property
        def board_attempt(self):
            raise RuntimeError("boom")

    tid = fixtures.acknowledged(conn)
    assert qr.attempt_timing(Hostile()) == {}
    qr.record_pull_error(conn, "hub", error=Hostile())
    value = _latest(conn, tid)
    assert value["code"] == "board_error" and not set(qr.TIMING_KEYS) & set(value)


def test_stamping_never_masks_and_the_innermost_attempt_wins():
    class Frozen(OSError):
        def __setattr__(self, name, value):
            raise AttributeError(name)

    frozen = Frozen()
    assert attempt.note_attempt(frozen, T0 + 1, T0, T0) is None  # swallowed: the caller re-raises the original
    inner = OSError()
    attempt.note_attempt(inner, T0 + 2, T0, T0)
    attempt.note_attempt(inner, T0 + 9, T0, T0 + 1)  # an outer attempt does not overwrite the redirect's own stamp
    assert getattr(inner, attempt.ATTEMPT_ATTR) == (2.0, 0.0)


def test_timing_never_makes_a_new_pull_event(conn):
    tid = fixtures.acknowledged(conn)
    for stamp in ((1.0, 1.0), (0.2, 0.2)):
        error = urllib.error.URLError(TimeoutError("timed out"))
        setattr(error, attempt.ATTEMPT_ATTR, stamp)
        qr.record_pull_error(conn, "hub", error=error)
    assert len(_pull_values(conn, tid)) == 1
