"""Negotiated deltas name exact gzip entity bytes, never decompressed JSON."""
from __future__ import annotations

import asyncio
import gzip
import json
import random

import pytest
from starlette.datastructures import Headers

from hermes_cli import web_delta_response as wdr

pytestmark = pytest.mark.skipif(wdr._zstd is None, reason="requires Python 3.14 zstd")
ASK = {"x-hermes-delta-accept": "zstd-gzip-v1"}


def transcript(version=0):
    rng = random.Random(47)
    rows = [{"role": "assistant", "content": rng.randbytes(400).hex()} for _ in range(200)]
    rows[-1]["content"] += str(version)
    return json.dumps({"messages": rows}, ensure_ascii=False).encode()


async def request(body, store, headers=None, *, coding="gzip", status=200, method="GET", length=None, chunks=None):
    start = {"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"), (b"content-encoding", coding.encode()),
        (b"vary", b"Accept-Encoding"), (b"etag", b'"entity"'),
        (b"cache-control", b"private"),
    ]}
    if length != "stream":
        start["headers"].append((b"content-length", str(len(body) if length is None else length).encode()))
    sent = []

    async def app(scope, receive, send):
        await send(start)
        parts = chunks if chunks is not None else [body]
        for i, chunk in enumerate(parts):
            await send({"type": "http.response.body", "body": chunk, "more_body": i < len(parts) - 1})

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": method, "path": "/api/sessions/example/messages",
             "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()]}
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await wdr.DeltaResponseMiddleware(app, store=store)(scope, receive, send)
    return Headers(raw=sent[0]["headers"]), b"".join(m.get("body", b"") for m in sent[1:]), sent


def get(body, store, headers=ASK, **kwargs):
    return asyncio.run(request(body, store, headers, **kwargs))


def restore(frame, base):
    return wdr._zstd.decompress(frame, zstd_dict=wdr._zstd.ZstdDict(base, is_raw=True).as_prefix)


def test_gzip_whole_unchanged_changed_exact_bytes_and_json():
    store = wdr.BodyStore()
    raw = transcript()
    entity = gzip.compress(raw, mtime=0)
    h, wire, _ = get(entity, store)
    assert wire == entity and h["content-encoding"] == "gzip"
    assert h[wdr.HASH_HEADER] == wdr.body_hash(entity)
    assert h["cache-control"] == "private" and h["etag"] == '"entity"'
    for next_raw in (raw, transcript(1)):
        next_entity = gzip.compress(next_raw, mtime=0)
        h, wire, _ = get(next_entity, store, {**ASK, wdr.BASE_HEADER: wdr.body_hash(entity)})
        assert h[wdr.DELTA_HEADER] == "zstd"
        assert h["x-hermes-delta-content-encoding"] == "gzip"
        assert "content-encoding" not in h and "etag" not in h
        assert h["cache-control"] == "no-store"
        assert {s.strip().lower() for s in h["vary"].split(",")} == {
            "accept-encoding", "x-hermes-delta-accept", "x-hermes-delta-base"}
        assert int(h["content-length"]) == len(wire) < len(next_entity)
        assert restore(wire, entity) == next_entity
        assert json.loads(gzip.decompress(restore(wire, entity))) == json.loads(next_raw)
        assert h[wdr.HASH_HEADER] == wdr.body_hash(next_entity)
        entity = next_entity


@pytest.mark.parametrize("accept", [None, "zstd", "zstd-gzip-v2", "gzip"])
def test_old_client_or_unknown_capability_keeps_original_gzip(accept):
    entity = gzip.compress(transcript(), mtime=0)
    store = wdr.BodyStore()
    headers = {} if accept is None else {"x-hermes-delta-accept": accept}
    h, wire, _ = get(entity, store, headers)
    assert wire == entity and h["content-encoding"] == "gzip"
    assert wdr.HASH_HEADER not in h and wdr.DELTA_HEADER not in h and len(store) == 0


@pytest.mark.parametrize("case", ["missing", "corrupt", "other-credential", "rolled"])
def test_gzip_base_credential_isolation_and_disk_verification(tmp_path, case):
    entity = gzip.compress(transcript(), mtime=0)
    root = tmp_path / "bases"
    store = wdr.BodyStore(disk_dir=root)
    alice = {**ASK, "authorization": "Bearer alice"}
    h, _, _ = get(entity, store, alice)
    digest = h[wdr.HASH_HEADER]
    if case == "missing":
        digest = "f" * 32
    elif case == "corrupt":
        [file] = list(root.iterdir())
        file.write_bytes(b"damaged gzip")
    headers = {**alice, wdr.BASE_HEADER: digest}
    if case == "other-credential":
        headers["authorization"] = "Bearer bob"
    h, wire, _ = get(entity, wdr.BodyStore(disk_dir=root), headers)
    if case == "rolled":
        assert h[wdr.DELTA_HEADER] == "zstd" and restore(wire, entity) == entity
    else:
        assert wdr.DELTA_HEADER not in h and wire == entity
    assert sum(p.stat().st_size for p in root.iterdir()) <= wdr.STORE_MAX_BYTES


@pytest.mark.parametrize("extra", [0, 1])
def test_compare_against_gzip_not_uncompressed_size(monkeypatch, extra):
    entity = gzip.compress(transcript(), mtime=0)
    store = wdr.BodyStore()
    h, _, _ = get(entity, store)
    monkeypatch.setattr(wdr, "encode_delta", lambda body, base: b"x" * (len(body) + extra))
    h, wire, _ = get(entity, store, {**ASK, wdr.BASE_HEADER: h[wdr.HASH_HEADER]})
    assert wire == entity and "content-encoding" in h and wdr.DELTA_HEADER not in h


@pytest.mark.parametrize("kwargs", [
    {"coding": "br"}, {"coding": "gzip, br"}, {"status": 206}, {"method": "POST"},
    {"method": "HEAD"}, {"length": "stream"}, {"length": 12}, {"length": wdr.MAX_BYTES + 1},
])
def test_gzip_ineligible_passthrough(kwargs):
    entity = gzip.compress(transcript(), mtime=0)
    store = wdr.BodyStore()
    h, wire, _ = get(entity, store, **kwargs)
    assert wire == entity and wdr.HASH_HEADER not in h and len(store) == 0


def test_encode_exception_is_original_gzip(monkeypatch):
    entity = gzip.compress(transcript(), mtime=0)
    store = wdr.BodyStore()
    h, _, _ = get(entity, store)

    def broken(body, base):
        raise RuntimeError("injected encode failure")

    monkeypatch.setattr(wdr, "encode_delta", broken)
    h, wire, _ = get(entity, store, {**ASK, wdr.BASE_HEADER: h[wdr.HASH_HEADER]})
    assert wire == entity and h["content-encoding"] == "gzip" and wdr.HASH_HEADER not in h


def test_dishonest_length_stops_buffering_at_declared_bound():
    """Even a never-ending oversized producer must release the held start before its last chunk."""
    entity = gzip.compress(transcript(), mtime=0)
    store = wdr.BodyStore()
    h, wire, sent = get(entity, store, length=wdr.MIN_BYTES,
                        chunks=[entity[:wdr.MIN_BYTES], entity[wdr.MIN_BYTES:], b"tail"])
    assert wire == entity + b"tail" and wdr.HASH_HEADER not in h and len(store) == 0
    assert sent[1]["more_body"] is True


def test_gzip_small_and_no_zstd_passthrough(monkeypatch):
    store = wdr.BodyStore()
    entity = gzip.compress(b'{"ok":true}', mtime=0)
    h, wire, _ = get(entity, store)
    assert wire == entity and wdr.HASH_HEADER not in h and len(store) == 0
    monkeypatch.setattr(wdr, "_zstd", None)
    entity = gzip.compress(transcript(), mtime=0)
    h, wire, _ = get(entity, store)
    assert wire == entity and wdr.HASH_HEADER not in h and len(store) == 0


def test_negotiated_gzip_eligibility_has_the_same_entity_bounds():
    def eligible(length, coding="gzip", content_type="application/json"):
        return wdr._eligible({"status": 200, "headers": [
            (b"content-length", str(length).encode()), (b"content-type", content_type.encode()),
            (b"content-encoding", coding.encode()),
        ]}, allow_gzip=True)

    assert eligible(wdr.MIN_BYTES) and eligible(wdr.MAX_BYTES)
    assert eligible(wdr.MIN_BYTES, " GZip ", "application/json; charset=utf-8")
    assert not eligible(wdr.MIN_BYTES - 1) and not eligible(wdr.MAX_BYTES + 1)
    assert not eligible(wdr.MIN_BYTES, "br") and not eligible(wdr.MIN_BYTES, content_type="text/plain")


def test_negotiated_gzip_uses_existing_body_and_frame_budgets(tmp_path):
    entity = gzip.compress(transcript(), mtime=0)
    store = wdr.BodyStore(max_bytes=len(entity), frames_max_bytes=1, disk_dir=tmp_path / "bases")
    h, _, _ = get(entity, store)
    h, wire, _ = get(entity, store, {**ASK, wdr.BASE_HEADER: h[wdr.HASH_HEADER]})
    assert restore(wire, entity) == entity
    assert store._bytes <= len(entity) and store._frames_bytes <= 1
    changed = gzip.compress(transcript(1), mtime=0)
    get(changed, store)
    assert store._bytes <= len(entity)
    assert sum(p.stat().st_size for p in (tmp_path / "bases").iterdir()) <= len(entity)
