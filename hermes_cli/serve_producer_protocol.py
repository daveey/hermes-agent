"""Bounded data framing for serve producer relays; no producer imports or I/O.

The caller owns authentication, scope generation and aggregate installation budgets.
A request is not visible to its consumer until its final COMMIT. JSON escapes retain
Python strings including lone surrogates, without changing public panel validators.
"""
from __future__ import annotations

import hashlib
import json
import struct
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

MAX_FRAME = 64 * 1024
MAX_REQUEST = 1024 * 1024
_CHUNK = 24 * 1024
_HEADER = struct.Struct('!I')


class ProtocolError(ValueError):
    """Malformed internal transport; disconnect rather than accepting partial work."""


class ResourceExhausted(ValueError):
    """Retryable overload, not invalid public input."""


def _reject_constant(value):
    raise ValueError(f'nonfinite JSON value: {value}')


class _Frame(dict):
    def __init__(self, value: dict, wire_bytes: int):
        super().__init__(value)
        self.wire_bytes = wire_bytes


def _json_parts(value: Any, depth: int = 0):
    if depth > 32:
        raise ProtocolError('JSON nesting limit')
    if isinstance(value, str):
        yield b'"'
        for offset in range(0, len(value), 1024):
            yield json.dumps(value[offset:offset + 1024], ensure_ascii=True)[1:-1].encode('ascii')
        yield b'"'
    elif isinstance(value, (list, tuple)):
        yield b'['
        for index, item in enumerate(value):
            if index:
                yield b','
            yield from _json_parts(item, depth + 1)
        yield b']'
    elif isinstance(value, dict):
        yield b'{'
        for index, (key, item) in enumerate(value.items()):
            if not isinstance(key, str):
                raise ProtocolError('JSON object key must be a string')
            if index:
                yield b','
            yield from _json_parts(key, depth + 1)
            yield b':'
            yield from _json_parts(item, depth + 1)
        yield b'}'
    else:
        yield json.dumps(value, allow_nan=False, separators=(',', ':')).encode('ascii')


def _bounded_json(value: Any, limit: int) -> bytes:
    output = bytearray()
    for part in _json_parts(value):
        if len(output) + len(part) > limit:
            raise ResourceExhausted('encoded request budget')
        output.extend(part)
    return bytes(output)


def _frame(value: dict) -> bytes:
    wire = _bounded_json(value, MAX_FRAME)
    return _HEADER.pack(len(wire)) + wire


def encode_request(request_id: str, payload: dict) -> tuple[bytes, ...]:
    """Prepare the entire bounded transaction before enqueue/HTTP acceptance.

    Chunk contents are already escaped ASCII JSON, but enclosing frame escaping
    still counts toward the frame limit. A caller reserves the returned wire bytes
    in its mailbox before acknowledging; this function has no partial side effects.
    """
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ProtocolError('request id limit')
    body = _bounded_json(payload, MAX_REQUEST)
    frames = [_frame({'verb': 'BEGIN', 'id': request_id, 'total': len(body)})]
    for offset in range(0, len(body), _CHUNK):
        frames.append(_frame({'verb': 'CHUNK', 'id': request_id, 'offset': offset,
                              'data': body[offset:offset + _CHUNK].decode('ascii')}))
    frames.append(_frame({'verb': 'COMMIT', 'id': request_id}))
    if sum(map(len, frames)) > MAX_REQUEST:
        raise ResourceExhausted('framed request budget')
    return tuple(frames)


class FrameDecoder:
    """Incremental length-prefixed stream parser with bounded retained state."""

    def __init__(self, *, control: bool = False):
        self._buffer = bytearray()
        self._expected = None
        self._limit = 8192 if control else MAX_FRAME
        self._closed = False

    def feed(self, data: bytes) -> list[dict]:
        if self._closed:
            raise ProtocolError('decoder closed')
        try:
            return self._feed(data)
        except ProtocolError:
            self._closed = True
            self._buffer.clear()
            raise

    def _feed(self, data: bytes) -> list[dict]:
        messages = []
        cursor = 0
        while cursor < len(data):
            need = (4 if self._expected is None else self._expected) - len(self._buffer)
            take = min(need, len(data) - cursor)
            self._buffer.extend(data[cursor:cursor + take])
            cursor += take
            if len(self._buffer) < (4 if self._expected is None else self._expected):
                continue
            if self._expected is None:
                self._expected = _HEADER.unpack(self._buffer)[0]
                self._buffer.clear()
                if not 1 <= self._expected <= self._limit:
                    raise ProtocolError('frame limit')
            else:
                try:
                    message = json.loads(self._buffer.decode('utf-8'), parse_constant=_reject_constant)
                except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                    raise ProtocolError('invalid frame JSON') from exc
                wire_bytes = self._expected + 4
                self._buffer.clear()
                self._expected = None
                if not isinstance(message, dict):
                    raise ProtocolError('frame must be an object')
                messages.append(_Frame(message, wire_bytes))
        return messages


@dataclass
class _Pending:
    request_id: str
    total: int
    body: bytearray
    framed_bytes: int


class RequestAssembly:
    """One authenticated client's atomic reassembly, bounded before allocation.

    Admission/control code aborts on revocation, expiry or disconnect. No callback
    runs here: only a completed object can enter the producer's demand mailbox.
    """

    def __init__(self):
        self._pending: _Pending | None = None
        self._completed: OrderedDict[str, tuple[int, bytes]] = OrderedDict()

    @property
    def reserved_bytes(self) -> int:
        return self._pending.total if self._pending is not None else 0

    def abort(self) -> None:
        self._pending = None

    def accept(self, message: dict) -> tuple[str, dict] | None:
        try:
            return self._accept(message)
        except (ProtocolError, ResourceExhausted):
            self.abort()
            raise

    def _accept(self, message: dict) -> tuple[str, dict] | None:
        verb = message.get('verb')
        request_id = message.get('id')
        wire_bytes = message.wire_bytes if isinstance(message, _Frame) else len(_frame(message))
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ProtocolError('request id limit')
        if verb == 'BEGIN':
            return self._begin(request_id, message.get('total'), wire_bytes)
        pending = self._pending
        if pending is None and verb == 'COMMIT' and request_id in self._completed:
            return None
        if pending is None or pending.request_id != request_id:
            raise ProtocolError('request not begun')
        pending.framed_bytes += wire_bytes
        if pending.framed_bytes > MAX_REQUEST:
            raise ResourceExhausted('framed request budget')
        if verb == 'CHUNK':
            self._chunk(pending, message)
            return None
        if verb != 'COMMIT':
            raise ProtocolError('unknown data verb')
        if len(pending.body) != pending.total:
            self.abort()
            raise ProtocolError('incomplete request')
        try:
            digest = hashlib.sha256(pending.body).digest()
            previous = self._completed.get(request_id)
            if previous is not None:
                if previous != (pending.total, digest):
                    raise ProtocolError('conflicting replay')
                return None
            payload = json.loads(pending.body.decode('ascii'), parse_constant=_reject_constant)
            if not isinstance(payload, dict):
                raise ProtocolError('request must be an object')
            self._completed[request_id] = (pending.total, digest)
            if len(self._completed) > 4096:
                self._completed.popitem(last=False)
            return request_id, payload
        except ProtocolError:
            raise
        except (ValueError, RecursionError) as exc:
            raise ProtocolError('invalid request JSON') from exc
        finally:
            self.abort()

    def _begin(self, request_id: str, total: Any, wire_bytes: int) -> None:
        if type(total) is not int or not 1 <= total <= MAX_REQUEST:
            raise ResourceExhausted('reassembly budget')
        previous = self._completed.get(request_id)
        if previous is not None and previous[0] != total:
            raise ProtocolError('conflicting replay')
        if self._pending is not None:
            raise ProtocolError('request already begun')
        self._pending = _Pending(request_id, total, bytearray(), wire_bytes)

    def _chunk(self, pending: _Pending, message: dict) -> None:
        offset, text = message.get('offset'), message.get('data')
        if type(offset) is not int or not isinstance(text, str) or not text.isascii():
            raise ProtocolError('invalid chunk')
        data = text.encode('ascii')
        if len(data) > _CHUNK or offset < 0 or offset + len(data) > pending.total:
            raise ProtocolError('chunk limit')
        if offset < len(pending.body) and pending.body[offset:offset + len(data)] == data:
            return  # exact retransmission is idempotent before COMMIT
        if offset != len(pending.body):
            raise ProtocolError('chunk gap or conflicting replay')
        pending.body.extend(data)
