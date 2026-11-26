"""Bounded release-only reply framing; create packets retain their old bytes."""
import hashlib
import json
import socket
import time

LIMIT = 65536
RELEASE_LIMIT = 2 * 1024 * 1024
CHUNK = 32768


def send(reply, result, *, release):
    data = json.dumps(result, separators=(",", ":")).encode("utf-8")
    bound = RELEASE_LIMIT if release else LIMIT
    if len(data) > bound:
        data = b'{"ok":false,"error":"CLI bridge reply exceeds bounded inventory capacity"}'
    if len(data) <= LIMIT:
        reply.send(data)
        return
    deadline = time.monotonic() + 2
    reply.settimeout(2)
    header = {"release_stream": 1, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    reply.send(json.dumps(header, separators=(",", ":")).encode("ascii"))
    for offset in range(0, len(data), CHUNK):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("release reply send deadline exceeded")
        reply.settimeout(remaining)
        reply.send(data[offset:offset + CHUNK])


def receive(reader, *, release, timeout):
    deadline = time.monotonic() + timeout
    response, _, flags, _ = reader.recvmsg(LIMIT)
    if flags & socket.MSG_TRUNC:
        raise ValueError("truncated CLI bridge reply")
    value = json.loads(response)
    if release and isinstance(value, dict) and "release_stream" in value:
        if set(value) != {"release_stream", "bytes", "sha256"}:
            raise ValueError("invalid release stream header")
        length = value["bytes"]
        if type(value["release_stream"]) is not int or value["release_stream"] != 1 or type(length) is not int or not LIMIT < length <= RELEASE_LIMIT:
            raise ValueError("invalid release stream bound")
        digest = value["sha256"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("invalid release stream digest")
        parts, size = [], 0
        while size < length:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ValueError("release stream deadline exceeded")
            reader.settimeout(left)
            part, _, flags, _ = reader.recvmsg(CHUNK)
            if not part or flags & socket.MSG_TRUNC or len(part) != min(CHUNK, length - size):
                raise ValueError("invalid or incomplete release stream")
            size += len(part)
            parts.append(part)
        data = b"".join(parts)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("release stream digest mismatch")
        value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("CLI bridge response is not an object")
    return value
