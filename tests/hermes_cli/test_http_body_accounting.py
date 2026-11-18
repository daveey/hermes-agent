"""Passive ASGI HTTP lifecycle / body-byte diagnostics (http_body_accounting)."""

from __future__ import annotations

import asyncio
import itertools
import threading
import time

import pytest

from hermes_cli import http_body_accounting as hba
from hermes_cli import loop_accounting as la
from hermes_cli.web_server_transport import transport_observed_app


COUNTERS = (
    "requests_started",
    "response_finished",
    "failed",
    "cancelled",
    "inflight",
    "request_body_bytes",
    "response_body_bytes",
)


@pytest.fixture(autouse=True)
def _fresh():
    hba.reset()
    la.reset()
    yield
    hba.reset()
    la.reset()


def _scope(method: str, path: str, *, query: bytes = b"", headers=None):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query,
        "headers": headers or [],
        "client": ("127.0.0.1", 9),
        "server": ("127.0.0.1", 80),
        "scheme": "http",
    }


def _receive_body(body: bytes = b"", *, disconnect: bool = False):
    sent = {"n": 0}

    async def receive():
        sent["n"] += 1
        if disconnect and sent["n"] == 1:
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


async def _ok_app(scope, receive, send, *, body=b"ok", status=200, chunks=None):
    await send({"type": "http.response.start", "status": status, "headers": []})
    if chunks is None:
        await send({"type": "http.response.body", "body": body, "more_body": False})
        return
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        await send(
            {"type": "http.response.body", "body": chunk, "more_body": not last}
        )


def _run(app, scope, receive=None, send=None):
    receive = receive or _receive_body()
    sent = []

    async def _send(message):
        sent.append(message)
        if send is not None:
            await send(message)

    async def main():
        await app(scope, receive, _send)

    asyncio.run(main())
    return sent


def _wrap(inner):
    return hba.wrap_http_body_accounting(inner)


def _route(snap, key):
    return snap["routes"][key]


def test_snapshot_shape_unit_and_other_zeros():
    snap = hba.snapshot()
    assert snap["unit"] == "ASGI body bytes"
    assert set(snap["routes"]) == {hba.OTHER}
    other = snap["routes"][hba.OTHER]
    assert tuple(other[k] for k in COUNTERS) == (0, 0, 0, 0, 0, 0, 0)
    assert isinstance(snap["monotonic_ns"], int) and snap["monotonic_ns"] > 0
    assert isinstance(snap["time_ns"], int) and snap["time_ns"] > 0


def test_match_route_uses_literal_and_placeholder_keys_not_raw_ids():
    sid = "sess_user_9f3a"
    assert hba.match_route("GET", "/api/sessions") == "GET /api/sessions"
    assert hba.match_route("GET", f"/api/sessions/{sid}") == (
        "GET /api/sessions/{session_id}"
    )
    assert hba.match_route("GET", "/api/sessions/stats") == "GET /api/sessions/stats"
    assert hba.match_route("GET", "/nope") is None
    # scope["path"] carries no query; '?' is an ordinary char, never split.
    assert hba.match_route("GET", "/api/sessions?x=1") is None
    assert sid not in (hba.match_route("GET", f"/api/sessions/{sid}") or "")


def test_allowlist_is_bounded_and_has_inspected_desktop_poll_routes():
    assert len(hba.ROUTE_ALLOWLIST) <= hba.MAX_ROUTE_KEYS
    keys = {f"{m} {p}" for m, p in hba.ROUTE_ALLOWLIST}
    for needed in (
        "GET /api/status",
        "GET /api/sessions",
        "GET /api/sessions/{session_id}",
        "GET /api/session-kanban-tasks",
        "GET /api/plugins/kanban/board",
        "GET /api/health",
        "POST /api/sessions/import",
    ):
        assert needed in keys
    for _method, path in hba.ROUTE_ALLOWLIST:
        assert path.startswith("/")
        assert "?" not in path
        assert "{" not in path or "}" in path
        assert ":path}" not in path  # catch-alls would emit unbounded keys
        assert not any(ch in path for ch in "=&")
    assert len(keys) == len(hba.ROUTE_ALLOWLIST) == len(set(hba.ROUTE_ALLOWLIST))
    assert hba.OTHER not in keys
    assert hba.MAX_ROUTE_KEYS == 64
    assert hba.OTHER == "other"


def test_successful_2xx_matched_route_migrates_off_other():
    async def app(scope, receive, send):
        await _ok_app(scope, receive, send, body=b"hello")

    _run(_wrap(app), _scope("GET", "/api/status"))
    snap = hba.snapshot()
    status = _route(snap, "GET /api/status")
    other = _route(snap, hba.OTHER)
    assert status["requests_started"] == 1
    assert status["response_finished"] == 1
    assert status["inflight"] == 0
    assert status["failed"] == 0
    assert status["cancelled"] == 0
    assert status["response_body_bytes"] == 5
    assert other["requests_started"] == 0
    assert other["inflight"] == 0
    assert other["response_finished"] == 0


def test_streaming_sums_response_bytes_across_chunks():
    chunks = [b"aa", b"bbb", b"c"]

    async def app(scope, receive, send):
        await _ok_app(scope, receive, send, chunks=chunks)

    _run(_wrap(app), _scope("GET", "/api/sessions"))
    rec = _route(hba.snapshot(), "GET /api/sessions")
    assert rec["response_finished"] == 1
    assert rec["response_body_bytes"] == sum(len(c) for c in chunks)


def test_concurrent_distinct_routes_do_not_share_counters():
    async def inner(scope, receive, send):
        await _ok_app(scope, receive, send, body=scope["path"].encode())

    wrapped = _wrap(inner)

    async def one(path):
        sent = []

        async def send(message):
            sent.append(message)

        await wrapped(_scope("GET", path), _receive_body(), send)

    async def both():
        await asyncio.gather(
            one("/api/status"),
            one("/api/sessions"),
        )

    asyncio.run(both())
    snap = hba.snapshot()
    a = _route(snap, "GET /api/status")
    b = _route(snap, "GET /api/sessions")
    assert a["requests_started"] == b["requests_started"] == 1
    assert a["response_finished"] == b["response_finished"] == 1
    assert a["response_body_bytes"] == len(b"/api/status")
    assert b["response_body_bytes"] == len(b"/api/sessions")
    assert _route(snap, hba.OTHER)["requests_started"] == 0


def test_unread_request_body_is_not_counted():
    body = b"x" * 17

    async def app(scope, receive, send):
        await _ok_app(scope, receive, send, body=b"y")

    _run(_wrap(app), _scope("POST", "/api/sessions/import"), _receive_body(body))
    rec = _route(hba.snapshot(), "POST /api/sessions/import")
    assert rec["request_body_bytes"] == 0
    assert rec["response_body_bytes"] == 1
    assert rec["response_finished"] == 1


def test_consumed_request_body_counts_only_received_http_request():
    body = b"abcdef"

    async def app(scope, receive, send):
        msg = await receive()
        assert msg["type"] == "http.request"
        await _ok_app(scope, receive, send, body=b"z")

    _run(_wrap(app), _scope("POST", "/api/sessions/import"), _receive_body(body))
    rec = _route(hba.snapshot(), "POST /api/sessions/import")
    assert rec["request_body_bytes"] == 6


def test_disconnect_without_final_body_is_cancelled():
    async def app(scope, receive, send):
        await receive()

    _run(
        _wrap(app),
        _scope("GET", "/api/status"),
        _receive_body(disconnect=True),
    )
    other = _route(hba.snapshot(), hba.OTHER)
    # No 2xx start, so attribution stays other.
    assert other["cancelled"] == 1
    assert other["failed"] == 0
    assert other["response_finished"] == 0
    assert other["inflight"] == 0
    assert other["requests_started"] == 1


def test_cancelled_error_is_distinct_from_failed():
    async def app(scope, receive, send):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        _run(_wrap(app), _scope("GET", "/api/status"))
    rec = _route(hba.snapshot(), hba.OTHER)
    assert rec["cancelled"] == 1
    assert rec["failed"] == 0
    assert rec["response_finished"] == 0
    assert rec["inflight"] == 0


def test_failed_send_does_not_count_unsent_bytes_or_finish():
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        await send({"type": "http.response.body", "body": b"tail", "more_body": False})

    n = {"body": 0}

    async def send(message):
        if message.get("type") == "http.response.body":
            n["body"] += 1
            if n["body"] == 2:
                raise ConnectionError("peer reset")

    with pytest.raises(ConnectionError):
        _run(_wrap(inner), _scope("GET", "/api/health"), send=send)
    rec = _route(hba.snapshot(), "GET /api/health")
    assert rec["failed"] == 1
    assert rec["response_finished"] == 0
    assert rec["cancelled"] == 0
    assert rec["inflight"] == 0
    assert rec["response_body_bytes"] == len(b"partial")


def test_aborted_return_without_final_body_is_failed():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x", "more_body": True})

    _run(_wrap(app), _scope("GET", "/api/status"))
    rec = _route(hba.snapshot(), "GET /api/status")
    assert rec["failed"] == 1
    assert rec["response_finished"] == 0
    assert rec["inflight"] == 0
    assert rec["response_body_bytes"] == 1


def test_terminal_finish_then_later_exception_does_not_double_count():
    async def app(scope, receive, send):
        await _ok_app(scope, receive, send, body=b"done")
        raise RuntimeError("after send")

    with pytest.raises(RuntimeError):
        _run(_wrap(app), _scope("GET", "/api/status"))
    rec = _route(hba.snapshot(), "GET /api/status")
    assert rec["response_finished"] == 1
    assert rec["failed"] == 0
    assert rec["cancelled"] == 0
    assert rec["inflight"] == 0


async def _blackhole(message):
    return None


def test_long_lived_stream_stays_inflight_until_final_body():
    started = asyncio.Event()
    finish = asyncio.Event()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ab", "more_body": True})
        started.set()
        await finish.wait()
        await send({"type": "http.response.body", "body": b"cd", "more_body": False})

    async def main():
        task = asyncio.create_task(
            _wrap(app)(_scope("GET", "/api/sessions"), _receive_body(), _blackhole)
        )
        await started.wait()
        mid = _route(hba.snapshot(), "GET /api/sessions")
        assert mid["inflight"] == 1
        assert mid["response_finished"] == 0
        assert mid["response_body_bytes"] == 2
        finish.set()
        await task

    asyncio.run(main())
    end = _route(hba.snapshot(), "GET /api/sessions")
    assert end["inflight"] == 0
    assert end["response_finished"] == 1
    assert end["response_body_bytes"] == 4


def test_unmatched_auth_404_and_redirect_normalize_to_other():
    cases = [
        ("GET", "/api/unknown/secret-user", 200, b"ok"),
        ("GET", "/api/status", 401, b"nope"),
        ("GET", "/api/sessions", 404, b"missing"),
        ("GET", "/api/health", 302, b""),
    ]

    async def app(scope, receive, send):
        status = {
            "/api/unknown/secret-user": 200,
            "/api/status": 401,
            "/api/sessions": 404,
            "/api/health": 302,
        }[scope["path"]]
        await _ok_app(scope, receive, send, status=status, body=b"x")

    wrapped = _wrap(app)
    for method, path, _status, _body in cases:
        _run(wrapped, _scope(method, path))
    snap = hba.snapshot()
    other = _route(snap, hba.OTHER)
    assert other["requests_started"] == 4
    assert other["response_finished"] == 4
    assert "GET /api/status" not in snap["routes"] or _route(
        snap, "GET /api/status"
    )["requests_started"] == 0
    blob = repr(snap)
    assert "secret-user" not in blob
    assert "?" not in "".join(snap["routes"])


def test_privacy_never_emits_raw_path_query_header_or_user_ids():
    headers = [(b"authorization", b"Bearer user-99"), (b"x-user-id", b"u-42")]
    path = "/api/sessions/session-abc-not-a-template"
    query = b"token=sekrit&user_id=u-42"

    async def app(scope, receive, send):
        await _ok_app(scope, receive, send, body=b"ok")

    _run(_wrap(app), _scope("GET", path, query=query, headers=headers))
    snap = hba.snapshot()
    keys = " ".join(snap["routes"])
    blob = repr(snap)
    assert "session-abc-not-a-template" not in keys
    assert "sekrit" not in blob
    assert "user-99" not in blob
    assert "u-42" not in blob
    assert "token=" not in blob
    assert _route(snap, "GET /api/sessions/{session_id}")["requests_started"] == 1


def test_websocket_and_lifespan_are_excluded():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])
        if scope["type"] == "websocket":
            await send({"type": "websocket.accept"})
            return
        await send({"type": "lifespan.startup.complete"})

    wrapped = _wrap(inner)

    async def go():
        async def receive():
            return {"type": "lifespan.startup"}

        async def send(_message):
            return None

        await wrapped({"type": "websocket", "path": "/api/ws"}, receive, send)
        await wrapped({"type": "lifespan"}, receive, send)

    asyncio.run(go())
    assert seen == ["websocket", "lifespan"]
    snap = hba.snapshot()
    assert snap["routes"][hba.OTHER]["requests_started"] == 0
    assert len(snap["routes"]) == 1


def test_snapshot_is_an_immutable_copy():
    async def app(scope, receive, send):
        await _ok_app(scope, receive, send)

    _run(_wrap(app), _scope("GET", "/api/status"))
    snap = hba.snapshot()
    snap["routes"]["GET /api/status"]["requests_started"] = 99
    snap["routes"][hba.OTHER]["failed"] = 7
    again = hba.snapshot()
    assert again["routes"]["GET /api/status"]["requests_started"] == 1
    assert again["routes"][hba.OTHER]["failed"] == 0


def test_reset_and_after_fork_clear_counters_and_replace_lock():
    async def app(scope, receive, send):
        await _ok_app(scope, receive, send)

    _run(_wrap(app), _scope("GET", "/api/status"))
    old_lock = hba._lock
    hba.reset()
    assert hba.snapshot()["routes"][hba.OTHER]["requests_started"] == 0
    assert "GET /api/status" not in hba.snapshot()["routes"]
    _run(_wrap(app), _scope("GET", "/api/sessions"))
    hba._after_fork_child()
    assert hba._lock is not old_lock
    snap = hba.snapshot()
    assert snap["routes"][hba.OTHER]["requests_started"] == 0
    assert "GET /api/sessions" not in snap["routes"]


def test_streaming_backpressure_is_not_buffered_by_the_wrapper():
    released = asyncio.Event()
    in_first_send = asyncio.Event()
    app_after_first = {"n": 0}

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"1", "more_body": True})
        app_after_first["n"] += 1
        await send({"type": "http.response.body", "body": b"2", "more_body": False})

    async def send(message):
        if message.get("type") == "http.response.body" and message.get("more_body"):
            in_first_send.set()
            await released.wait()

    async def main():
        task = asyncio.create_task(
            _wrap(inner)(_scope("GET", "/api/status"), _receive_body(), send)
        )
        await in_first_send.wait()
        assert app_after_first["n"] == 0
        released.set()
        await task

    asyncio.run(main())
    rec = _route(hba.snapshot(), "GET /api/status")
    assert rec["response_finished"] == 1
    assert rec["response_body_bytes"] == 2


def test_outer_wrapper_does_not_open_a_loop_accounting_route_scope():
    async def inner(scope, receive, send):
        with la.pass_("route/sessions.kanban_tasks"):
            await _ok_app(scope, receive, send, body=b"[]")

    _run(_wrap(inner), _scope("GET", "/api/session-kanban-tasks"))
    loops = la.counters()
    assert loops["route/sessions.kanban_tasks"]["runs"] == 1
    assert not any(
        name.startswith("http") or "ASGI" in name or "/api/" in name
        for name in loops
    )


def test_transport_observed_app_wraps_http_and_keeps_raw_transport():
    seen = {}

    async def inner(scope, receive, send):
        seen["raw"] = scope.get("hermes.raw_transport")
        await _ok_app(scope, receive, send, body=b"ok")

    app = transport_observed_app(
        inner, proxy_headers=False, forwarded_allow_ips="127.0.0.1"
    )
    _run(app, _scope("GET", "/api/status"))
    assert seen["raw"] == (("127.0.0.1", 9), ("127.0.0.1", 80))
    rec = _route(hba.snapshot(), "GET /api/status")
    assert rec["requests_started"] == 1
    assert rec["response_finished"] == 1


def test_thread_safe_snapshot_during_concurrent_requests():
    async def inner(scope, receive, send):
        await _ok_app(scope, receive, send, body=b"z")

    wrapped = _wrap(inner)
    errors = []

    def worker():
        try:
            for _ in range(20):
                asyncio.run(
                    wrapped(
                        _scope("GET", "/api/health"),
                        _receive_body(),
                        _blackhole,
                    )
                )
                hba.snapshot()
        except Exception as exc:  # health: allow BLE001 -- collect for the parent thread
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    rec = _route(hba.snapshot(), "GET /api/health")
    assert rec["requests_started"] == 80
    assert rec["response_finished"] == 80
    assert rec["inflight"] == 0


def test_measured_wrap_overhead_fixture():
    async def inner(scope, receive, send):
        await _ok_app(scope, receive, send, body=b"ok")

    wrapped = _wrap(inner)
    scope = _scope("GET", "/api/status")
    n = 2000
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        for _ in range(n):
            asyncio.run(wrapped(scope, _receive_body(), _blackhole))
        best = min(best, (time.perf_counter() - t0) / n)
    # Pin "not milliseconds"; matching + two locked updates is cheap.
    assert best < 5e-3, f"{best * 1e6:.1f} µs per wrapped request"


def _assert_monotonic_and_conserved(prev, cur):
    for key in COUNTERS:
        if key == "inflight":
            continue
        assert cur["totals"][key] >= prev["totals"][key], key
        for name, row in cur["routes"].items():
            before = prev["routes"].get(name, hba._zero())
            assert row[key] >= before[key], (name, key)
    for snap in (prev, cur):
        inflight = sum(r["inflight"] for r in snap["routes"].values())
        assert snap["totals"]["inflight"] == (
            snap["unclassified_inflight"] + inflight
        )


def test_snapshots_around_response_start_are_monotonic_and_conserved():
    before_start = asyncio.Event()
    go = asyncio.Event()
    after_start = asyncio.Event()
    finish = asyncio.Event()

    async def app(scope, receive, send):
        await receive()
        before_start.set()
        await go.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ab", "more_body": True})
        after_start.set()
        await finish.wait()
        await send({"type": "http.response.body", "body": b"c", "more_body": False})

    snaps = []

    async def main():
        task = asyncio.create_task(
            _wrap(app)(
                _scope("POST", "/api/sessions/import"),
                _receive_body(b"12345"),
                _blackhole,
            )
        )
        await before_start.wait()
        snaps.append(hba.snapshot())
        go.set()
        await after_start.wait()
        snaps.append(hba.snapshot())
        finish.set()
        await task
        snaps.append(hba.snapshot())

    asyncio.run(main())
    s0, s1, s2 = snaps
    assert s0["unclassified_inflight"] == 1
    assert s0["totals"]["inflight"] == 1
    assert s0["totals"]["requests_started"] == 1
    assert s0["totals"]["request_body_bytes"] == 5
    assert s0["routes"][hba.OTHER]["requests_started"] == 0
    assert s0["routes"][hba.OTHER]["request_body_bytes"] == 0
    key = "POST /api/sessions/import"
    assert s1["unclassified_inflight"] == 0
    assert s1["routes"][key]["inflight"] == 1
    assert s1["routes"][key]["requests_started"] == 1
    assert s1["routes"][key]["request_body_bytes"] == 5
    assert s1["totals"]["request_body_bytes"] == 5  # not doubled
    assert s1["totals"]["response_body_bytes"] == 2
    assert s1["routes"][hba.OTHER]["requests_started"] == 0
    assert s2["totals"]["response_finished"] == 1
    assert s2["totals"]["inflight"] == 0
    assert s2["totals"]["response_body_bytes"] == 3
    assert s2["totals"]["request_body_bytes"] == 5
    for a, b in itertools.pairwise(snaps):
        _assert_monotonic_and_conserved(a, b)


def test_terminal_before_start_books_other_once_and_totals_match():
    async def app(scope, receive, send):
        await receive()
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _run(_wrap(app), _scope("POST", "/api/sessions/import"), _receive_body(b"xyz"))
    snap = hba.snapshot()
    other = _route(snap, hba.OTHER)
    assert other["requests_started"] == 1 and other["failed"] == 1
    assert other["request_body_bytes"] == 3
    assert snap["totals"]["request_body_bytes"] == 3
    assert snap["totals"]["failed"] == 1
    assert snap["unclassified_inflight"] == 0
    assert "POST /api/sessions/import" not in snap["routes"]


def test_non_2xx_start_classifies_other_without_negative_counts():
    async def app(scope, receive, send):
        await receive()
        await _ok_app(scope, receive, send, status=404, body=b"no")

    _run(_wrap(app), _scope("POST", "/api/sessions/import"), _receive_body(b"abc"))
    snap = hba.snapshot()
    other = _route(snap, hba.OTHER)
    assert (other["requests_started"], other["request_body_bytes"]) == (1, 3)
    assert snap["totals"]["requests_started"] == 1
    assert all(v >= 0 for row in snap["routes"].values() for v in row.values())


def test_route_cap_overflow_goes_to_other_and_totals_conserve():
    with hba._lock:
        for i in range(hba.MAX_ROUTE_KEYS):
            hba._routes[f"GET /fake/{i}"] = hba._zero()

    async def app(scope, receive, send):
        await _ok_app(scope, receive, send, body=b"x")

    _run(_wrap(app), _scope("GET", "/api/status"))
    snap = hba.snapshot()
    assert "GET /api/status" not in snap["routes"]
    assert _route(snap, hba.OTHER)["requests_started"] == 1
    assert snap["totals"]["requests_started"] == 1


def test_match_route_path_only_semantics():
    assert hba.match_route("GET", "/api/sessions/") == "GET /api/sessions"
    assert hba.match_route("GET", "//api/sessions") is None
    assert hba.match_route("GET", "/api//sessions") is None
    assert hba.match_route("GET", "/api/sessions//stats") is None
    assert hba.match_route("GET", "/api/sessions/abc//messages") is None
    assert hba.match_route("GET", "/") is None
    assert hba.match_route("GET", "") is None
