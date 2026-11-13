"""Delta responses (hermes_cli/web_delta_response.py): the exact handler bytes, as a zstd delta when asked."""

from __future__ import annotations

import gzip
import json

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from hermes_cli import web_delta_response as wdr

try:
    from compression import zstd
except ImportError:  # Python < 3.14: the middleware passes everything through, which two tests below still check
    zstd = None

needs_zstd = pytest.mark.skipif(zstd is None, reason="compression.zstd needs Python 3.14+")

ASK = {"X-Hermes-Delta-Accept": "zstd"}


def board(count: int, done: int = -1) -> dict:
    return {"cards": [{"id": i, "status": "done" if i == done else "todo", "title": f"card {i}"} for i in range(count)]}


def decode(frame: bytes, base: bytes) -> bytes:
    return zstd.decompress(frame, zstd_dict=zstd.ZstdDict(base, is_raw=True).as_prefix)


@pytest.fixture
def served():
    """A tiny app whose /board answer the test can change, behind the middleware."""
    state = {"board": board(2000), "calls": 0}

    async def get_board(request):
        state["calls"] += 1
        return JSONResponse(state["board"])

    async def small(request):
        return JSONResponse({"ok": True})

    async def text(request):
        return PlainTextResponse("x" * 50_000)

    async def missing(request):
        return JSONResponse({"detail": "x" * 50_000}, status_code=404)

    async def zipped(request):
        body = gzip.compress(json.dumps(state["board"]).encode())
        return Response(body, media_type="application/json", headers={"content-encoding": "gzip"})

    async def stream(request):
        async def chunks():
            yield b'{"big":"' + b"x" * 50_000 + b'"}'

        return StreamingResponse(chunks(), media_type="application/json")

    async def chunked(request):
        body = json.dumps(state["board"]).encode()

        async def chunks():
            for at in range(0, len(body), 7000):
                yield body[at:at + 7000]

        # A Content-Length with the body in many chunks (what a file response does).
        return StreamingResponse(chunks(), media_type="application/json", headers={"content-length": str(len(body))})

    async def cookie(request):
        response = JSONResponse(state["board"], headers={"cache-control": "no-store", "etag": '"abc"'})
        response.set_cookie("a", "1")
        response.set_cookie("b", "2")
        return response

    app = Starlette(routes=[
        Route("/board", get_board, methods=["GET", "POST", "HEAD"]),
        Route("/small", small), Route("/text", text), Route("/missing", missing),
        Route("/zipped", zipped), Route("/stream", stream), Route("/chunked", chunked), Route("/cookie", cookie),
    ])
    store = wdr.BodyStore()
    app.add_middleware(wdr.DeltaResponseMiddleware, store=store)
    return TestClient(app), state, store


def test_unasked_request_is_untouched(served):
    client, state, store = served
    response = client.get("/board")
    assert response.json() == state["board"]
    assert wdr.HASH_HEADER not in response.headers and wdr.DELTA_HEADER not in response.headers
    assert len(store) == 0


@needs_zstd
def test_first_ask_gets_the_whole_body_with_its_hash(served):
    client, state, store = served
    response = client.get("/board", headers=ASK)
    assert response.headers["content-type"] == "application/json"
    assert response.json() == state["board"]
    assert response.headers[wdr.HASH_HEADER] == wdr.body_hash(response.content)
    assert wdr.DELTA_HEADER not in response.headers
    assert len(store) == 1


@needs_zstd
def test_known_base_gets_a_small_delta_that_decodes_to_the_exact_current_body(served):
    client, state, _ = served
    first = client.get("/board", headers=ASK)
    state["board"] = board(2001, done=1234)

    second = client.get("/board", headers={**ASK, "X-Hermes-Delta-Base": first.headers[wdr.HASH_HEADER]})

    assert second.status_code == 200
    assert second.headers[wdr.DELTA_HEADER] == "zstd"
    assert second.headers[wdr.BASE_HEADER] == first.headers[wdr.HASH_HEADER]
    assert second.headers["content-type"] == wdr.DELTA_MEDIA_TYPE
    assert int(second.headers["content-length"]) == len(second.content)
    current = decode(second.content, first.content)
    assert json.loads(current) == state["board"]
    assert second.headers[wdr.HASH_HEADER] == wdr.body_hash(current)
    assert len(second.content) < len(current) / 50
    assert state["calls"] == 2  # the handler ran both times: the answer is never served from the store

    # The delta itself can be the next base.
    state["board"] = board(2001, done=77)
    third = client.get("/board", headers={**ASK, "X-Hermes-Delta-Base": second.headers[wdr.HASH_HEADER]})
    assert json.loads(decode(third.content, current)) == state["board"]


@needs_zstd
def test_unchanged_body_is_a_tiny_delta(served):
    client, _, _ = served
    first = client.get("/board", headers=ASK)
    again = client.get("/board", headers={**ASK, "X-Hermes-Delta-Base": first.headers[wdr.HASH_HEADER]})
    assert again.headers[wdr.DELTA_HEADER] == "zstd"
    assert len(again.content) < 200
    assert decode(again.content, first.content) == first.content


@needs_zstd
@pytest.mark.parametrize("base", ["f" * 32, "", "not-a-hash"])
def test_unknown_base_gets_the_whole_body(served, base):
    client, state, _ = served
    response = client.get("/board", headers={**ASK, "X-Hermes-Delta-Base": base})
    assert wdr.DELTA_HEADER not in response.headers
    assert response.json() == state["board"]
    assert response.headers[wdr.HASH_HEADER] == wdr.body_hash(response.content)


@needs_zstd
def test_base_evicted_from_the_store_gets_the_whole_body(served):
    client, state, store = served
    first = client.get("/board", headers=ASK)
    store._bodies.clear()  # a restart, or the store rolled past it
    store._bytes = 0
    response = client.get("/board", headers={**ASK, "X-Hermes-Delta-Base": first.headers[wdr.HASH_HEADER]})
    assert wdr.DELTA_HEADER not in response.headers
    assert response.json() == state["board"]


@needs_zstd
@pytest.mark.parametrize("path", ["/small", "/text", "/missing", "/zipped", "/stream"])
def test_ineligible_responses_pass_through_untouched(served, path):
    """Small, non-JSON, non-200, already encoded and streamed answers are never hashed, stored or encoded."""
    client, _, store = served
    plain = client.get(path)
    asked = client.get(path, headers={**ASK, "X-Hermes-Delta-Base": "f" * 32})
    assert asked.status_code == plain.status_code
    assert asked.content == plain.content
    assert asked.headers.get("content-type") == plain.headers.get("content-type")
    assert wdr.HASH_HEADER not in asked.headers and wdr.DELTA_HEADER not in asked.headers
    assert len(store) == 0


@needs_zstd
def test_a_write_is_never_encoded(served):
    client, state, store = served
    response = client.post("/board", headers={**ASK, "X-Hermes-Delta-Base": "f" * 32})
    assert response.json() == state["board"]
    assert wdr.HASH_HEADER not in response.headers
    assert len(store) == 0


@needs_zstd
def test_a_delta_no_smaller_than_the_body_is_not_sent(served, monkeypatch):
    client, state, _ = served
    first = client.get("/board", headers=ASK)
    monkeypatch.setattr(wdr, "encode_delta", lambda body, base: body + b"!")
    response = client.get("/board", headers={**ASK, "X-Hermes-Delta-Base": first.headers[wdr.HASH_HEADER]})
    assert wdr.DELTA_HEADER not in response.headers
    assert response.json() == state["board"]


def test_without_zstd_every_request_passes_through(served, monkeypatch):
    client, state, store = served
    monkeypatch.setattr(wdr, "_zstd", None)
    response = client.get("/board", headers=ASK)
    assert response.json() == state["board"]
    assert wdr.HASH_HEADER not in response.headers
    assert len(store) == 0


def test_store_is_bounded_by_bytes_and_drops_the_least_recently_used():
    store = wdr.BodyStore(max_bytes=3000)
    store.put("a", b"a" * 1000)
    store.put("b", b"b" * 1000)
    store.put("c", b"c" * 1000)
    assert store.get("a") is not None  # a is now the most recently used
    store.put("d", b"d" * 1000)
    assert store.get("b") is None
    assert store.get("a") is not None and store.get("c") is not None and store.get("d") is not None
    store.put("a", b"ignored: same hash is the same bytes")
    assert store.get("a") == b"a" * 1000


@needs_zstd
def test_frame_matches_the_fixture_the_desktop_test_decodes():
    """apps/desktop/electron/delta-reads.test.ts decodes this exact frame with Node's zstd: keep the two in step."""
    old = json.dumps(board(40), separators=(",", ":")).encode()
    new = json.dumps(board(41, done=7), separators=(",", ":")).encode()
    assert wdr.body_hash(old) == "534e8f50b28c0e95528daf1f36a43a0c"
    assert wdr.body_hash(new) == "611f65f648a6b3b611140694997e4b95"
    assert decode(wdr.encode_delta(new, old), old) == new


def test_the_dashboard_app_serves_through_the_middleware():
    from hermes_cli import web_server

    assert any(m.cls is wdr.DeltaResponseMiddleware for m in web_server.app.user_middleware)


@needs_zstd
def test_chunked_body_with_a_content_length_is_encoded_as_one(served):
    client, state, _ = served
    first = client.get("/chunked", headers=ASK)
    assert first.json() == state["board"]
    state["board"] = board(2001, done=5)
    second = client.get("/chunked", headers={**ASK, "X-Hermes-Delta-Base": first.headers[wdr.HASH_HEADER]})
    assert second.headers[wdr.DELTA_HEADER] == "zstd"
    assert json.loads(decode(second.content, first.content)) == state["board"]


@needs_zstd
def test_other_headers_survive_a_delta_duplicates_included(served):
    client, _, _ = served
    first = client.get("/cookie", headers=ASK)
    client.cookies.clear()  # the test client would send them back, which is another credential (see below)
    second = client.get("/cookie", headers={**ASK, "X-Hermes-Delta-Base": first.headers[wdr.HASH_HEADER]})
    assert second.headers[wdr.DELTA_HEADER] == "zstd"
    assert second.headers["cache-control"] == "no-store" and second.headers["etag"] == '"abc"'
    assert sorted(second.headers.get_list("set-cookie"))[0].startswith("a=1")
    assert len(second.headers.get_list("set-cookie")) == 2


def test_head_is_untouched(served):
    client, _, store = served
    response = client.head("/board", headers=ASK)
    assert wdr.HASH_HEADER not in response.headers
    assert len(store) == 0


@needs_zstd
def test_a_base_is_only_usable_by_the_credential_it_was_served_to(served):
    """The delta's length depends on its base: one credential must not be able to probe another's answers."""
    client, state, store = served
    alice = {**ASK, "X-Hermes-Session-Token": "alice"}
    first = client.get("/board", headers=alice)
    digest = first.headers[wdr.HASH_HEADER]

    for other in ({"X-Hermes-Session-Token": "bob"}, {"Authorization": "Bearer bob"}, {"Cookie": "s=bob"}, {}):
        probe = client.get("/board", headers={**ASK, **other, "X-Hermes-Delta-Base": digest})
        assert wdr.DELTA_HEADER not in probe.headers, other
        assert probe.json() == state["board"]

    again = client.get("/board", headers={**alice, "X-Hermes-Delta-Base": digest})
    assert again.headers[wdr.DELTA_HEADER] == "zstd"
    # The same bytes are held once per credential that was sent them (alice, three bobs, no credential).
    assert len(store) == 5


def test_credential_namespace_separates_credentials_and_is_empty_without_one():
    from starlette.datastructures import Headers

    def ns(**headers):
        return wdr.credential_namespace(Headers(headers))

    assert ns() == ""
    assert ns(**{"x-hermes-session-token": "a"}) == ns(**{"x-hermes-session-token": "a", "accept": "x"})
    assert len({ns(**{"x-hermes-session-token": "a"}), ns(**{"x-hermes-session-token": "b"}),
                ns(authorization="a"), ns(cookie="a"), ns()}) == 5


@needs_zstd
def test_encoding_failure_sends_the_handler_answer_unchanged(served, monkeypatch):
    client, state, _ = served
    first = client.get("/board", headers=ASK)

    def boom(body, base):
        raise RuntimeError("zstd exploded")

    monkeypatch.setattr(wdr, "encode_delta", boom)
    response = client.get("/board", headers={**ASK, "X-Hermes-Delta-Base": first.headers[wdr.HASH_HEADER]})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert wdr.DELTA_HEADER not in response.headers and wdr.HASH_HEADER not in response.headers
    assert response.json() == state["board"]


@needs_zstd
@pytest.mark.parametrize("megabytes", [0.5, 3.5])
def test_a_one_field_change_stays_tiny_past_the_default_zstd_window(megabytes):
    """At the default 2 MiB window a 3.5 MB body with one change encoded to 36 KB (measured 2026-11-13)."""
    def body(done):
        rows = [{"id": i, "status": "done" if i == done else "todo", "body": "lorem ipsum dolor " * 6}
                for i in range(int(megabytes * 7000))]
        return json.dumps({"cards": rows}).encode()

    old, new = body(-1), body(1000)
    assert len(new) > megabytes * 0.9e6
    frame = wdr.encode_delta(new, old)
    assert len(frame) < 2000
    assert decode(frame, old) == new


def test_size_bounds_of_what_is_encoded():
    def start(length, **over):
        headers = {"content-type": "application/json; charset=utf-8", "content-length": str(length), **over}
        return {"type": "http.response.start", "status": 200,
                "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}

    assert wdr._eligible(start(wdr.MIN_BYTES)) and wdr._eligible(start(wdr.MAX_BYTES))
    assert not wdr._eligible(start(wdr.MIN_BYTES - 1)) and not wdr._eligible(start(wdr.MAX_BYTES + 1))
    assert not wdr._eligible(start(50_000, **{"content-encoding": "gzip"}))
    assert not wdr._eligible(start(50_000, **{"content-type": "text/html"}))
    assert not wdr._eligible({**start(50_000), "status": 206})
    assert not wdr._eligible({**start(50_000), "headers": [(b"content-type", b"application/json")]})


def test_the_middleware_sits_inside_every_auth_gate_of_the_dashboard_app():
    """Starlette runs the last-added middleware first: auth gates are added after it, so they answer a refused
    request before this middleware ever sees one."""
    from hermes_cli import web_server

    chain = [m.cls for m in web_server.app.user_middleware]
    at = chain.index(wdr.DeltaResponseMiddleware)
    outer = chain[:at]
    assert len(outer) >= 5
    assert web_server.ChatFileUploadLimitMiddleware in chain[at + 1:]
