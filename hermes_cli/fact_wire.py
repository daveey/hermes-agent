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


def pack(value):
    if is_dataclass(value) and _TYPES.get(type(value).__name__) is type(value):
        return ["record", type(value).__name__, [pack(getattr(value, f.name)) for f in fields(value)]]
    if isinstance(value, Mapping):
        return ["map", [[pack(k), pack(v)] for k, v in value.items()]]
    if isinstance(value, bytes):
        return ["bytes", [base64.b64encode(value[i:i + 65536]).decode("ascii")
                          for i in range(0, len(value), 65536)]]
    if isinstance(value, (tuple, list)):
        return ["tuple", [pack(v) for v in value]]
    if isinstance(value, (set, frozenset)):
        return ["set", [pack(v) for v in value]]
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


def encoded_size(value):
    return sum(len(part) for part in json.JSONEncoder(
        ensure_ascii=True, separators=(",", ":"), allow_nan=False).iterencode(pack(value)))


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
