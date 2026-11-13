"""A polled route's delta frame is encoded once per change of its body, and a change is served on the next poll.

Card t_01a1d59ed9eb75fe (design t_01a1d563324d7e8e §6, serve residue): every Desktop poll of an unchanged
route re-ran the zstd encode against the same base. [daveey]
"""

from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from hermes_cli import loop_accounting as la
from hermes_cli import web_delta_response as wdr
from tests.hermes_cli.test_web_delta_response import ASK, board, decode, needs_zstd


@pytest.fixture
def served():
    """A /board answer the test can change, behind the middleware (as in test_web_delta_response)."""
    state = {"board": board(2000)}

    async def get_board(request):
        return JSONResponse(state["board"])

    app = Starlette(routes=[Route("/board", get_board)])
    store = wdr.BodyStore()
    app.add_middleware(wdr.DeltaResponseMiddleware, store=store)
    return TestClient(app), state, store


def _encodes(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = wdr.encode_delta
    monkeypatch.setattr(wdr, "encode_delta", lambda body, base: calls.append(len(body)) or real(body, base))
    return calls


@needs_zstd
def test_polls_of_an_unchanged_route_encode_once(served, monkeypatch):
    client, _, _ = served
    calls = _encodes(monkeypatch)
    first = client.get("/board", headers=ASK)
    base = {**ASK, "X-Hermes-Delta-Base": first.headers[wdr.HASH_HEADER]}
    polls = [client.get("/board", headers=base) for _ in range(4)]
    assert len(calls) == 1
    for poll in polls:
        assert poll.headers[wdr.DELTA_HEADER] == "zstd"
        assert decode(poll.content, first.content) == first.content


@needs_zstd
def test_a_changed_body_is_encoded_and_served_on_the_next_poll(served, monkeypatch):
    client, state, _ = served
    calls = _encodes(monkeypatch)
    first = client.get("/board", headers=ASK)
    base = {**ASK, "X-Hermes-Delta-Base": first.headers[wdr.HASH_HEADER]}
    client.get("/board", headers=base)
    state["board"] = board(2000, done=7)
    changed = client.get("/board", headers=base)
    assert len(calls) == 2
    assert json.loads(decode(changed.content, first.content)) == state["board"]
    assert changed.headers[wdr.HASH_HEADER] != first.headers[wdr.HASH_HEADER]
    again = client.get("/board", headers=base)
    assert len(calls) == 2
    assert again.content == changed.content


@needs_zstd
def test_the_memo_is_per_credential_and_per_base(served, monkeypatch):
    client, state, _ = served
    calls = _encodes(monkeypatch)
    alice = {**ASK, "Authorization": "Bearer alice"}
    bob = {**ASK, "Authorization": "Bearer bob"}
    a0 = client.get("/board", headers=alice)
    b0 = client.get("/board", headers=bob)
    assert a0.headers[wdr.HASH_HEADER] == b0.headers[wdr.HASH_HEADER]
    client.get("/board", headers={**alice, "X-Hermes-Delta-Base": a0.headers[wdr.HASH_HEADER]})
    client.get("/board", headers={**bob, "X-Hermes-Delta-Base": b0.headers[wdr.HASH_HEADER]})
    assert len(calls) == 2  # same body and base, other credential: its own encode
    state["board"] = board(2000, done=3)
    a1 = client.get("/board", headers=alice)
    client.get("/board", headers={**alice, "X-Hermes-Delta-Base": a1.headers[wdr.HASH_HEADER]})
    client.get("/board", headers={**alice, "X-Hermes-Delta-Base": a0.headers[wdr.HASH_HEADER]})
    assert len(calls) == 4  # another base for the same body is another frame


@needs_zstd
def test_an_evicted_base_still_gets_the_whole_body_after_a_memo_hit(served):
    client, state, store = served
    first = client.get("/board", headers=ASK)
    base = {**ASK, "X-Hermes-Delta-Base": first.headers[wdr.HASH_HEADER]}
    assert wdr.DELTA_HEADER in client.get("/board", headers=base).headers
    store._bodies.clear()
    store._bytes = 0
    response = client.get("/board", headers=base)
    assert wdr.DELTA_HEADER not in response.headers
    assert response.json() == state["board"]


def test_frame_memo_is_bounded_by_bytes_and_entries(monkeypatch):
    monkeypatch.setattr(wdr, "FRAMES_MAX_ENTRIES", 3)
    store = wdr.BodyStore(frames_max_bytes=100)
    for i in range(5):
        store.put_frame(f"k{i}", b"x" * 10)
    assert [store.frame(f"k{i}")[0] for i in range(5)] == [False, False, True, True, True]
    store.put_frame("big", b"x" * 101)
    assert store.frame("big") == (False, None)
    store.put_frame("none", None)
    assert store.frame("none") == (True, None)
    for i in range(10):
        store.put_frame(f"m{i}", b"y" * 40)
    assert store._frames_bytes <= 100


@needs_zstd
def test_memo_hits_are_p1_skips(served):
    client, _, _ = served
    before = la.counters().get("delta.encode", {"runs": 0, "skips": 0})
    first = client.get("/board", headers=ASK)
    base = {**ASK, "X-Hermes-Delta-Base": first.headers[wdr.HASH_HEADER]}
    for _ in range(3):
        client.get("/board", headers=base)
    after = la.counters()["delta.encode"]
    assert after["runs"] - before["runs"] == 4
    assert after["skips"] - before["skips"] == 2
