"""Private JSON transport for the existing fact contract (never pickle). [daveey]"""
from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from typing import Any

from hermes_cli import fact_contract as contract

_TYPES: dict[str, Any] = {cls.__name__: cls for cls in (
    contract.Scope, contract.SourceId, contract.FactKey, contract.Fact,
    contract.Position, contract.Delta,
)}


_CHUNK = 65536


def pack(value):
    return _pack(value, None)


def _pack(value, bytes_sizes):
    """Transport form of *value*. With a *bytes_sizes* list, bytes pack as an empty chunk list and the length
    their chunks would have added to the encoding is appended to *bytes_sizes* (sizing without base64)."""
    if is_dataclass(value) and _TYPES.get(type(value).__name__) is type(value):
        return ["record", type(value).__name__,
                [_pack(getattr(value, f.name), bytes_sizes) for f in fields(value)]]
    if isinstance(value, Mapping):
        return ["map", [[_pack(k, bytes_sizes), _pack(v, bytes_sizes)] for k, v in value.items()]]
    if isinstance(value, bytes):
        if bytes_sizes is not None:
            # Each chunk encodes as a quoted base64 string ("..." = 4 per 3 bytes, rounded up), comma-separated.
            whole, tail = divmod(len(value), _CHUNK)
            chunks = [4 * -(-_CHUNK // 3)] * whole + ([4 * -(-tail // 3)] if tail else [])
            bytes_sizes.append(sum(chunks) + 2 * len(chunks) + max(len(chunks) - 1, 0))
            return ["bytes", []]
        return ["bytes", [base64.b64encode(value[i:i + _CHUNK]).decode("ascii")
                          for i in range(0, len(value), _CHUNK)]]
    if isinstance(value, (tuple, list)):
        return ["tuple", [_pack(v, bytes_sizes) for v in value]]
    if isinstance(value, (set, frozenset)):
        return ["set", [_pack(v, bytes_sizes) for v in value]]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(type(value).__name__)


def unpack(value) -> Any:
    if not isinstance(value, list):
        return value
    tag = value[0]
    if tag == "record":
        return _TYPES[value[1]](*(unpack(v) for v in value[2]))
    if tag == "map":
        return {unpack(k): unpack(v) for k, v in value[1]}
    if tag == "bytes":
        return b"".join(base64.b64decode(chunk, validate=True) for chunk in value[1])
    if tag == "tuple":
        return tuple(unpack(v) for v in value[1])
    if tag == "set":
        return frozenset(unpack(v) for v in value[1])
    raise ValueError("unknown fact transport tag")


def encode(value) -> bytes:
    return json.dumps(pack(value), ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def decode(data: bytes | bytearray) -> Any:
    return unpack(json.loads(data))


_SIZER = json.JSONEncoder(ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def encoded_size(value):
    """len(encode(value)), without building the encoding's bytes or any base64. [daveey]

    JSONEncoder.iterencode() always runs json's pure-Python encoder; sizing every changed fact that way was ~25%
    of the hub's fact-change-feed thread (2026-11-23). This uses the C one-shot encoder instead (ASCII-only, so
    characters == bytes), and counts bytes payloads arithmetically so a large bytes view adds no transient copy.
    """
    bytes_sizes = []
    return len(_SIZER.encode(_pack(value, bytes_sizes))) + sum(bytes_sizes)


def encode_into(value, stream, limit):
    """Spool reset bytes without retaining a second full encoded generation."""
    size = 0
    for part in json.JSONEncoder(ensure_ascii=True, separators=(",", ":"), allow_nan=False).iterencode(pack(value)):
        block = part.encode("ascii")
        size += len(block)
        if size > limit:
            raise ValueError("reset_budget_exceeded")
        stream.write(block)
    return size
