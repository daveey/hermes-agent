"""Boundary predicates reject malformed frames without consuming admission."""
import json
import socket

import pytest

from tools import origin_cli_bridge as bridge
from tests.tools import test_origin_cli_bridge as helpers

echo_launch = helpers.echo_launch


@pytest.mark.parametrize("flag", [socket.MSG_TRUNC, socket.MSG_CTRUNC])
def test_truncated_request_never_dispatches(echo_launch, flag):
    reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with reader, writer:
        fds = [writer.fileno()]
        echo_launch._handle(bridge._encode({"nonce": echo_launch._nonce, "args": {"title": "x"}}), flag, fds)
        assert fds == [writer.fileno()]
        assert echo_launch.retire() == []


@pytest.mark.parametrize("count", [0, 2])
def test_wrong_descriptor_count_is_rejected(echo_launch, count):
    readers, writers = [], []
    try:
        for _ in range(count):
            reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            readers.append(reader)
            writers.append(writer)
        fds = [writer.fileno() for writer in writers]
        echo_launch._handle(b'{}', 0, fds)
        assert fds == [writer.fileno() for writer in writers]
        assert echo_launch.retire() == []
    finally:
        for sock in readers + writers:
            sock.close()


@pytest.mark.parametrize("value", [[], {"args": []}, {"args": "bad"}])
def test_malformed_payload_is_rejected_before_dispatch(echo_launch, value):
    if isinstance(value, dict):
        value["nonce"] = echo_launch._nonce
    reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with reader:
        fds = [writer.detach()]
        echo_launch._handle(json.dumps(value).encode(), 0, fds)
        assert fds == []
        reader.settimeout(1)
        assert reader.recv(4096) == b''
        assert echo_launch.retire() == []


@pytest.mark.parametrize("family,kind", [(socket.AF_UNIX, socket.SOCK_STREAM), (socket.AF_INET, socket.SOCK_DGRAM)])
def test_wrong_reply_socket_is_rejected(echo_launch, family, kind):
    sock = socket.socket(family, kind)
    fds = [sock.detach()]
    echo_launch._handle(bridge._encode({"nonce": echo_launch._nonce, "args": {"title": "x"}}), 0, fds)
    assert fds == []
    assert echo_launch.retire() == []


@pytest.mark.parametrize("side,family,kind", [
    ("reply", socket.AF_INET, socket.SOCK_SEQPACKET),
    ("reply", socket.AF_UNIX, socket.SOCK_STREAM),
    ("channel", socket.AF_INET, socket.SOCK_SEQPACKET),
])
def test_socket_predicates_stop_at_first_boundary(monkeypatch, echo_launch, side, family, kind):
    class Socket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def settimeout(self, timeout):
            pass
    fake = Socket()
    fake.family, fake.type = family, kind
    def forbidden(*args):
        pytest.fail("invalid socket crossed its validation boundary")
    monkeypatch.setattr(bridge.socket, "socket", lambda **kw: fake)
    if side == "reply":
        monkeypatch.setattr(bridge.select, "poll", forbidden)
        echo_launch._handle(bridge._encode({"nonce": echo_launch._nonce, "args": {}}), 0, [42])
        assert echo_launch.retire() == []
    else:
        monkeypatch.setattr(bridge.os, "dup", lambda fd: 42)
        monkeypatch.setattr(bridge.socket, "socketpair", forbidden)
        assert bridge.request(json.dumps({"fd": 42, "nonce": "x"}), {})["outcome"] == "outcome_unknown"


def test_encode_limit_is_enforced():
    with pytest.raises(ValueError, match="too large"):
        bridge._encode("x" * bridge.LIMIT)
    assert len(bridge._encode("x" * (bridge.LIMIT - 2))) == bridge.LIMIT


@pytest.mark.parametrize("fd,nonce", [(True, "x"), (-1, "x"), (3, 42)])
def test_invalid_locator_never_duplicates(monkeypatch, fd, nonce):
    def forbidden(*args):
        pytest.fail("invalid locator reached descriptor duplication")
    monkeypatch.setattr(bridge.os, "dup", forbidden)
    assert bridge.request(json.dumps({"fd": fd, "nonce": nonce}), {})["outcome"] == "outcome_unknown"


@pytest.mark.parametrize("family,kind", [(socket.AF_UNIX, socket.SOCK_STREAM), (socket.AF_INET, socket.SOCK_DGRAM)])
def test_wrong_client_socket_never_opens_reply(monkeypatch, family, kind):
    with socket.socket(family, kind) as sock:
        def forbidden(*args):
            pytest.fail("invalid channel reached reply creation")
        monkeypatch.setattr(bridge.socket, "socketpair", forbidden)
        assert bridge.request(json.dumps({"fd": sock.fileno(), "nonce": "x"}), {})["outcome"] == "outcome_unknown"


@pytest.mark.parametrize("payload,flags", [(b'{}', socket.MSG_TRUNC), (b'[]', 0)])
def test_invalid_response_is_unknown(monkeypatch, payload, flags):
    class Socket:
        family = socket.AF_UNIX
        type = socket.SOCK_SEQPACKET
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def settimeout(self, timeout):
            pass
        def fileno(self):
            return 42
        def sendmsg(self, *args):
            pass
        def close(self):
            pass
        def recvmsg(self, *args):
            return payload, [], flags, None
    monkeypatch.setattr(bridge.os, "dup", lambda fd: 42)
    monkeypatch.setattr(bridge.socket, "socket", lambda **kw: Socket())
    monkeypatch.setattr(bridge.socket, "socketpair", lambda *args: (Socket(), Socket()))
    assert bridge.request(json.dumps({"fd": 42, "nonce": "x"}), {})["outcome"] == "outcome_unknown"
