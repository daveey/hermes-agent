"""Real seqpacket framing corruption and explicit capacity refusals."""
import hashlib
import json
import socket
from threading import Thread

import pytest

from tools import origin_cli_reply as reply


def test_real_large_seqpacket_roundtrip_and_legacy_bytes():
    value = {"ok": True, "comments": "x" * 150000}
    reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    errors = []
    def send():
        try:
            reply.send(writer, value, release=True)
        except (OSError, ValueError) as exc:
            errors.append(exc)
        finally:
            writer.close()
    thread = Thread(target=send)
    with reader:
        thread.start()
        try:
            assert reply.receive(reader, release=True, timeout=2) == value
        finally:
            thread.join(3)
    assert not thread.is_alive() and not errors
    reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with reader, writer:
        reply.send(writer, {"ok": True, "task_id": "legacy"}, release=False)
        assert reader.recv(reply.LIMIT) == b'{"ok":true,"task_id":"legacy"}'


@pytest.mark.parametrize("release,size", [(False, 70000), (True, reply.RELEASE_LIMIT + 1)])
def test_oversize_reply_is_explicit_small_refusal_not_silent_timeout(release, size):
    reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with reader, writer:
        reply.send(writer, {"ok": True, "body": "x" * size}, release=release)
        result = reply.receive(reader, release=release, timeout=1)
        assert result["ok"] is False and "capacity" in result["error"]


@pytest.mark.parametrize("change", [{"release_stream": True}, {"release_stream": 2}, {"bytes": True},
                                    {"bytes": reply.LIMIT}, {"bytes": reply.RELEASE_LIMIT + 1},
                                    {"sha256": "x"}, {"extra": 1}])
def test_invalid_stream_header_refuses_before_read(change):
    reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with reader, writer:
        header = {"release_stream": 1, "bytes": 70000, "sha256": "a" * 64, **change}
        writer.send(json.dumps(header).encode())
        with pytest.raises(ValueError, match="stream"):
            reply.receive(reader, release=True, timeout=1)


@pytest.mark.parametrize("kind", ["digest", "short", "reorder", "duplicate", "truncated", "eof", "deadline"])
def test_corrupt_stream_is_never_partial_success(kind):
    raw = json.dumps({"ok": True, "body": "a" * 40000 + "b" * 40000}).encode()
    reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    chunks = [raw[i:i + reply.CHUNK] for i in range(0, len(raw), reply.CHUNK)]
    header = {"release_stream": 1, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if kind == "digest":
        header["sha256"] = "0" * 64
    if kind == "reorder":
        chunks[0], chunks[1] = chunks[1], chunks[0]
    if kind == "duplicate":
        chunks[1] = chunks[0]
    if kind == "short":
        chunks[0] = chunks[0][:-1]
    if kind == "truncated":
        chunks[0] += b"extra"
    with reader, writer:
        writer.send(json.dumps(header).encode())
        if kind != "deadline":
            for part in chunks if kind != "eof" else []:
                writer.send(part)
            writer.shutdown(socket.SHUT_WR)
        with pytest.raises((ValueError, TimeoutError)):
            reply.receive(reader, release=True, timeout=0 if kind == "deadline" else 1)


def test_first_packet_truncation_and_nonobject_refuse():
    for raw in (b"x" * (reply.LIMIT + 1), b"[]"):
        reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with reader, writer:
            writer.send(raw)
            with pytest.raises(ValueError):
                reply.receive(reader, release=True, timeout=1)


def test_server_send_uses_total_deadline(monkeypatch):
    times = iter([1.0, 4.0])
    monkeypatch.setattr(reply.time, "monotonic", lambda: next(times))
    reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with reader, writer:
        with pytest.raises(ValueError, match="send deadline"):
            reply.send(writer, {"body": "x" * 70000}, release=True)
