"""Delta responses (hermes_cli/web_delta_response.py): the exact handler bytes, as a zstd delta when asked."""

from __future__ import annotations

import asyncio
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
        response = JSONResponse(state["board"], headers={"cache-control": "private, max-age=5", "etag": '"abc"'})
        response.set_cookie("a", "1")
        response.set_cookie("b", "2")
        return response

    async def varied(request):
        response = JSONResponse(state["board"], headers={"etag": '"abc"'})
        response.headers.append("vary", "Accept-Encoding, x-hermes-delta-base")
        response.headers.append("vary", "Origin")
        return response

    async def cached(request):
        return JSONResponse(state["board"], headers={"cache-control": "max-age=60"})

    async def cached_twice(request):
        response = JSONResponse(state["board"], headers={"cache-control": "public"})
        response.headers.append("cache-control", "max-age=60")
        return response

    app = Starlette(routes=[
        Route("/varied", varied), Route("/cached", cached), Route("/cached-twice", cached_twice),
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
    # The whole answer keeps the handler's Cache-Control; a delta is no-store (see the Cache-Control test below).
    assert first.headers["cache-control"] == "private, max-age=5"
    assert second.headers.get_list("cache-control") == ["no-store"]
    assert first.headers["etag"] == '"abc"' and "etag" not in second.headers  # a frame is not the handler's entity
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


def _start(body: bytes) -> dict:
    return {"type": "http.response.start", "status": 200, "headers": [
        (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]}


async def _run(app, headers) -> list[dict]:
    """What the server is sent for one GET through the middleware, on a scope that advertises pathsend."""
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "GET", "path": "/file", "extensions": {"http.response.pathsend": {}},
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    await wdr.DeltaResponseMiddleware(app, store=wdr.BodyStore())(scope, receive, send)
    return sent


@needs_zstd
@pytest.mark.parametrize("kind", ["http.response.pathsend", "http.response.zerocopysend"])
def test_a_file_the_server_sends_itself_still_gets_its_start_first(kind):
    """An eligible start is held for its body; a pathsend/zerocopysend is not a body, so the start must go first."""
    body = json.dumps(board(2000)).encode()
    start = _start(body)
    file_message = {"type": kind, "path": "/tmp/board.json"}

    async def app(scope, receive, send):
        await send(start)
        await send(file_message)

    assert asyncio.run(_run(app, {**ASK, "X-Hermes-Delta-Base": "f" * 32})) == [start, file_message]
    assert asyncio.run(_run(app, {})) == [start, file_message]  # exactly what an unasked request gets


@needs_zstd
def test_body_collected_before_a_non_body_message_goes_out_after_the_start():
    body = json.dumps(board(2000)).encode()
    start = _start(body)
    head = {"type": "http.response.body", "body": body[:9000], "more_body": True}
    file_message = {"type": "http.response.pathsend", "path": "/tmp/rest.json"}
    tail = {"type": "http.response.body", "body": b"", "more_body": False}

    async def app(scope, receive, send):
        for message in (start, head, file_message, tail):
            await send(message)

    assert asyncio.run(_run(app, ASK)) == [start, head, file_message, tail]


@needs_zstd
def test_hashed_and_delta_answers_vary_on_the_ask_and_a_delta_drops_the_etag(served):
    """A caching proxy must not replay a delta (or a hashed answer) to a client that did not ask for one."""
    client, _, _ = served
    first = client.get("/board", headers=ASK)
    assert first.headers.get_list("vary") == ["X-Hermes-Delta-Accept, X-Hermes-Delta-Base"]
    second = client.get("/board", headers={**ASK, "X-Hermes-Delta-Base": first.headers[wdr.HASH_HEADER]})
    assert second.headers[wdr.DELTA_HEADER] == "zstd"
    assert second.headers.get_list("vary") == ["X-Hermes-Delta-Accept, X-Hermes-Delta-Base"]

    # The handler's own Vary names are kept, once each, in one header; a whole body keeps its ETag, a delta does not.
    merged = ["Accept-Encoding, x-hermes-delta-base, Origin, X-Hermes-Delta-Accept"]
    whole = client.get("/varied", headers=ASK)
    assert wdr.DELTA_HEADER not in whole.headers
    assert whole.headers.get_list("vary") == merged
    assert whole.headers["etag"] == '"abc"'
    delta = client.get("/varied", headers={**ASK, "X-Hermes-Delta-Base": whole.headers[wdr.HASH_HEADER]})
    assert delta.headers[wdr.DELTA_HEADER] == "zstd"
    assert delta.headers.get_list("vary") == merged
    assert "etag" not in delta.headers

    # An unasked request, and an asked one that is not encoded, keep exactly the handler's headers.
    plain = client.get("/varied")
    assert plain.headers.get_list("vary") == ["Accept-Encoding, x-hermes-delta-base", "Origin"]
    assert plain.headers["etag"] == '"abc"'
    assert "vary" not in client.get("/small", headers=ASK).headers


def test_vary_star_is_left_alone():
    from starlette.datastructures import MutableHeaders

    headers = MutableHeaders(raw=[(b"vary", b"*")])
    wdr._merge_vary(headers)
    assert headers.getlist("vary") == ["*"]


@needs_zstd
@pytest.mark.parametrize(
    "path, handler_says",
    [("/cached", ["max-age=60"]), ("/cached-twice", ["public", "max-age=60"]), ("/board", [])],
)
def test_a_delta_is_no_store_and_a_whole_answer_keeps_the_handlers_cache_control(served, path, handler_says):
    """Cloudflare ignores Vary names other than Accept-Encoding; Cache-Control is what it honors.

    A delta is only meaningful to the one client holding its base, so no cache may store it. A whole hashed body is
    the handler's entity, so it keeps the handler's Cache-Control, and an unasked answer keeps it byte for byte."""
    client, _, _ = served
    plain = client.get(path)
    assert wdr.HASH_HEADER not in plain.headers
    assert plain.headers.get_list("cache-control") == handler_says
    whole = client.get(path, headers=ASK)
    assert wdr.DELTA_HEADER not in whole.headers
    assert whole.headers.get_list("cache-control") == handler_says
    delta = client.get(path, headers={**ASK, "X-Hermes-Delta-Base": whole.headers[wdr.HASH_HEADER]})
    assert delta.headers[wdr.DELTA_HEADER] == "zstd"
    assert delta.headers.get_list("cache-control") == ["no-store"]


# ── persisted bases (BodyStore disk_dir): a rolled or restarted backend still decodes against what was sent ──


def _served_by(store):
    state = {"board": board(2000)}

    async def get_board(request):
        return JSONResponse(state["board"])

    app = Starlette(routes=[Route("/board", get_board)])
    app.add_middleware(wdr.DeltaResponseMiddleware, store=store)
    return TestClient(app), state


ALICE = {**ASK, "X-Hermes-Session-Token": "alice"}


@needs_zstd
def test_a_base_survives_a_store_reopen_and_gives_a_verified_delta(tmp_path):
    client, state = _served_by(wdr.BodyStore(disk_dir=tmp_path))
    first = client.get("/board", headers=ALICE)
    digest = first.headers[wdr.HASH_HEADER]

    rolled, state = _served_by(wdr.BodyStore(disk_dir=tmp_path))  # a new backend process, nothing in memory
    state["board"] = board(2001, done=3)
    answer = rolled.get("/board", headers={**ALICE, "X-Hermes-Delta-Base": digest})

    assert answer.headers[wdr.DELTA_HEADER] == "zstd"
    assert len(answer.content) < len(first.content) / 50
    current = decode(answer.content, first.content)
    assert json.loads(current) == state["board"]
    assert answer.headers[wdr.HASH_HEADER] == wdr.body_hash(current)


@needs_zstd
@pytest.mark.parametrize("damage", ["truncate", "flip", "empty"])
def test_a_damaged_persisted_base_gives_the_whole_body_and_is_removed(tmp_path, damage):
    client, _ = _served_by(wdr.BodyStore(disk_dir=tmp_path))
    first = client.get("/board", headers=ALICE)
    digest = first.headers[wdr.HASH_HEADER]
    [path] = [p for p in tmp_path.iterdir() if p.name.endswith(digest)]
    data = path.read_bytes()
    flipped = data[:100] + bytes([data[100] ^ 1]) + data[101:]
    path.write_bytes({"truncate": data[: len(data) // 2], "flip": flipped, "empty": b""}[damage])

    other, state = _served_by(wdr.BodyStore(disk_dir=tmp_path))
    answer = other.get("/board", headers={**ALICE, "X-Hermes-Delta-Base": digest})
    assert wdr.DELTA_HEADER not in answer.headers
    assert answer.json() == state["board"]
    assert answer.headers[wdr.HASH_HEADER] == wdr.body_hash(answer.content)
    # The damaged file is gone; the whole body just sent took its place under the same (correct) digest.
    assert path.read_bytes() == answer.content


@needs_zstd
def test_another_credential_cannot_use_a_persisted_base(tmp_path):
    client, _ = _served_by(wdr.BodyStore(disk_dir=tmp_path))
    digest = client.get("/board", headers=ALICE).headers[wdr.HASH_HEADER]

    for other in ({"X-Hermes-Session-Token": "bob"}, {"Authorization": "Bearer alice"}, {}):
        fresh, _ = _served_by(wdr.BodyStore(disk_dir=tmp_path))
        probe = fresh.get("/board", headers={**ASK, **other, "X-Hermes-Delta-Base": digest})
        assert wdr.DELTA_HEADER not in probe.headers, other
    fresh, _ = _served_by(wdr.BodyStore(disk_dir=tmp_path))
    assert fresh.get("/board", headers={**ALICE, "X-Hermes-Delta-Base": digest}).headers[wdr.DELTA_HEADER] == "zstd"


def test_persisted_files_are_private_and_never_hold_the_credential(tmp_path):
    root = tmp_path / "delta-bodies"
    store = wdr.BodyStore(disk_dir=root)
    namespace = "a" * 32
    body = b'{"x":"' + b"y" * 20000 + b'"}'
    store.put(f"{namespace}:{wdr.body_hash(body)}", body)
    [path] = list(root.iterdir())
    assert path.name == f"{namespace}-{wdr.body_hash(body)}"
    assert path.stat().st_mode & 0o777 == 0o600
    assert root.stat().st_mode & 0o777 == 0o700
    assert path.read_bytes() == body


def test_the_disk_is_bounded_least_recently_used_first(tmp_path):
    import os
    import time

    tmp_path = tmp_path / "bases"  # the conftest's HERMES_HOME files live in tmp_path itself
    store = wdr.BodyStore(max_bytes=3000, disk_dir=tmp_path)
    bodies = {name: name.encode() * 1000 for name in "abcd"}
    keys = {name: f":{wdr.body_hash(body)}" for name, body in bodies.items()}
    for at, name in enumerate("abc"):
        store.put(keys[name], bodies[name])
        os.utime(tmp_path / f"_-{keys[name][1:]}", ns=(at * 10**9, at * 10**9))
    reopened = wdr.BodyStore(max_bytes=3000, disk_dir=tmp_path)
    assert reopened.get(keys["a"]) == bodies["a"]  # read from disk: a is now the most recently used
    assert (tmp_path / f"_-{keys['a'][1:]}").stat().st_mtime > time.time() - 60
    reopened.put(keys["d"], bodies["d"])
    names = {p.name for p in tmp_path.iterdir()}
    assert f"_-{keys['b'][1:]}" not in names
    assert {f"_-{keys[n][1:]}" for n in "acd"} <= names
    assert sum(p.stat().st_size for p in tmp_path.iterdir()) <= 3000


def test_a_base_memory_keeps_serving_stays_on_disk_for_the_next_backend(tmp_path, monkeypatch):
    """The disk budget must not drop the base polled most while newer bodies churn past it."""
    import os

    root = tmp_path / "bases"
    monkeypatch.setattr(wdr, "DISK_TOUCH_S", 0.0)
    store = wdr.BodyStore(max_bytes=3000, disk_dir=root)
    bodies = {name: name.encode() * 1000 for name in "habcdef"}
    keys = {name: f":{wdr.body_hash(body)}" for name, body in bodies.items()}

    def file(name):
        return root / f"_-{keys[name][1:]}"

    store.put(keys["h"], bodies["h"])
    for at, name in enumerate("abcdef", start=1):
        os.utime(file("h"), ns=(0, 0))  # as old as a file gets: only a refresh keeps it
        assert store.get(keys["h"]) == bodies["h"]  # the poll: a memory hit
        store.put(keys[name], bodies[name])
        os.utime(file(name), ns=(at * 10**9, at * 10**9))
    assert file("h").exists()
    assert wdr.BodyStore(max_bytes=3000, disk_dir=root).get(keys["h"]) == bodies["h"]  # the rolled backend
    # dropped by the budget anyway (another backend's churn): the next use writes it again
    file("h").unlink()
    store.put(keys["h"], bodies["h"])  # served again, unchanged
    assert file("h").read_bytes() == bodies["h"]
    assert sum(p.stat().st_size for p in root.iterdir()) <= 3000


def test_the_disk_refresh_is_rate_limited_and_forgets_bases_memory_dropped(tmp_path):
    import os

    root = tmp_path / "bases"
    store = wdr.BodyStore(max_bytes=3000, disk_dir=root)
    body = b"h" * 1000
    key = f":{wdr.body_hash(body)}"
    store.put(key, body)
    os.utime(root / f"_-{key[1:]}", ns=(0, 0))
    store.get(key)
    store.put(key, body)
    assert (root / f"_-{key[1:]}").stat().st_mtime_ns == 0  # within DISK_TOUCH_S: no syscall per poll
    for fill in b"abc":
        other = bytes([fill]) * 1000
        store.put(f":{wdr.body_hash(other)}", other)
    assert key not in store._touched and len(store._touched) == len(store) == 3


@pytest.mark.parametrize("key", ["../../etc:" + "a" * 32, "ns:" + "../x", ":" + "A" * 32, "x" * 32 + "/y:" + "a" * 32])
def test_a_key_that_is_not_two_hex_hashes_is_never_a_path(tmp_path, key):
    store = wdr.BodyStore(disk_dir=tmp_path / "d")
    store.put(key, b"z" * 20000)
    assert store.get(key) == b"z" * 20000  # memory still holds it
    assert not (tmp_path / "d").exists() or not any((tmp_path / "d").iterdir())
    assert wdr.BodyStore(disk_dir=tmp_path / "d").get(key) is None


def test_the_dashboard_store_persists_under_the_hermes_home_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = wdr.DeltaResponseMiddleware(lambda *a: None).store
    body = b"q" * 20000
    store.put(f":{wdr.body_hash(body)}", body)
    assert (tmp_path / "cache" / "delta-bodies" / f"_-{wdr.body_hash(body)}").read_bytes() == body


def test_an_unwritable_directory_leaves_the_store_in_memory(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    store = wdr.BodyStore(disk_dir=blocker / "sub")
    body = b"w" * 20000
    store.put(f":{wdr.body_hash(body)}", body)
    assert store.get(f":{wdr.body_hash(body)}") == body
