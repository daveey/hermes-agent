"""Synthetic, public gzip-entity delta benchmark (Python 3.14+).

Run: scripts/hermes-python scripts/bench_gzip_transcript_delta.py
Reports entity/frame body bytes (not HTTP/TLS overhead) and process CPU ms/op.
JSON generation is outside measurement; gzip, server hash/store/encode and client
zstd/hash/gunzip/JSON are measured separately. No production transcripts are read.
"""
from __future__ import annotations

import gzip
import json
import random
import statistics
import time

from hermes_cli import web_delta_response as wdr


def page():
    rng = random.Random(2026)
    return {"messages": [{"id": i, "role": "assistant", "content":
             "Synthetic transcript text. " * 30 + rng.randbytes(2500).hex()}
             for i in range(200)]}


def cpu_trial(fn, count=30):
    samples = []
    result = None
    for _ in range(count):
        start = time.process_time_ns()
        result = fn()
        samples.append((time.process_time_ns() - start) / 1e6)
    return result, {"median": round(statistics.median(samples), 4), "max": round(max(samples), 4), "n": count}


def main():
    original = page()
    appended = {"messages": [*original["messages"], {"id": 200, "role": "assistant", "content": "new synthetic message"}]}
    changed = page()
    changed["messages"][100]["content"] += " changed in the middle"
    raw = [json.dumps(p, ensure_ascii=False, separators=(",", ":")).encode() for p in (original, appended, changed)]
    entities = []
    gzip_cpu = []
    for body in raw:
        entity, cpu = cpu_trial(lambda body=body: gzip.compress(body, mtime=0))
        entities.append(entity)
        gzip_cpu.append(cpu)
    base = entities[0]
    digest = wdr.body_hash(base)
    results = []
    for name, body, entity, gzip_ms in zip(("unchanged", "appended", "middle-edit"),
                                         (raw[0], raw[1], raw[2]), entities, gzip_cpu):
        store = wdr.BodyStore()
        store.put(f"bench:{digest}", base)
        # Fresh frame memo for each trial: measure first encode, not a disguised memo hit.
        def cold():
            store._frames.clear()
            store._frames_bytes = 0
            return wdr._encode(entity, digest, "bench", store)
        (new_hash, frame), encode_ms = cpu_trial(cold)
        _, memo_ms = cpu_trial(lambda: wdr._encode(entity, digest, "bench", store))
        wire = entity if frame is None else frame

        def client():
            rebuilt = entity if frame is None else wdr._zstd.decompress(
                frame, zstd_dict=wdr._zstd.ZstdDict(base, is_raw=True).as_prefix)
            assert wdr.body_hash(rebuilt) == new_hash and rebuilt == entity
            restored = gzip.decompress(rebuilt)
            assert restored == body
            return json.loads(restored)

        decoded, decode_ms = cpu_trial(client)
        assert decoded == json.loads(body)
        _, whole_decode_ms = cpu_trial(lambda: json.loads(gzip.decompress(entity)))
        results.append({"case": name, "json_bytes": len(body), "gzip_bytes": len(entity),
                        "wire_bytes": len(wire), "delta": frame is not None,
                        "gzip_cpu_ms": gzip_ms, "server_first_encode_cpu_ms": encode_ms,
                        "server_memo_cpu_ms": memo_ms, "client_decode_verify_cpu_ms": decode_ms,
                        "whole_gzip_client_cpu_ms": whole_decode_ms})
    print(json.dumps({"units": "body bytes; process CPU milliseconds/op", "results": results}, indent=2))


if __name__ == "__main__":
    main()
