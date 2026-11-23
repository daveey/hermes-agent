"""Bounded helper transport preserves the panel's existing Python-string domain."""
import json
import struct

import pytest

from hermes_cli import serve_producer_protocol as protocol


@pytest.mark.parametrize('session', ['a' * 300, 'a' * 512, '\U0001f600' * 512, '\ud800' * 512, '\x00' * 512])
def test_chunked_request_roundtrips_exact_python_strings(session):
    request = {'sessions': [session, 'x' * 70000], 'subscriber': 's' * 128}
    frames = protocol.encode_request('request-1', request)
    assert all(len(frame) <= protocol.MAX_FRAME + 4 for frame in frames)
    decoder = protocol.FrameDecoder()
    assembly = protocol.RequestAssembly()
    accepted = []
    for frame in frames:
        # Real stream fragmentation: a frame header and body need not arrive together.
        for offset in range(0, len(frame), 137):
            for message in decoder.feed(frame[offset:offset + 137]):
                result = assembly.accept(message)
                if result is not None:
                    accepted.append(result)
    assert accepted == [('request-1', request)]
    assert assembly.reserved_bytes == 0


def test_control_decoder_rejects_data_sized_frame_at_header():
    decoder = protocol.FrameDecoder(control=True)
    with pytest.raises(protocol.ProtocolError, match='frame limit'):
        decoder.feed(struct.pack('!I', 8193))


def test_atomic_commit_replay_cannot_publish_twice():
    messages = [json.loads(frame[4:]) for frame in protocol.encode_request('same', {'sessions': ['a']})]
    assembly = protocol.RequestAssembly()
    completed = []
    for message in messages + messages:
        result = assembly.accept(message)
        if result is not None:
            completed.append(result)
    assert completed == [('same', {'sessions': ['a']})]
    with pytest.raises(protocol.ProtocolError, match='conflicting replay'):
        assembly.accept({'verb': 'BEGIN', 'id': 'same', 'total': 3})
        assembly.accept({'verb': 'CHUNK', 'id': 'same', 'offset': 0, 'data': '{} '})
        assembly.accept({'verb': 'COMMIT', 'id': 'same'})


def test_same_length_conflicting_commit_is_rejected_without_delivery():
    assembly = protocol.RequestAssembly()
    for frame in protocol.encode_request('same', {'a': 1}):
        assembly.accept(json.loads(frame[4:]))
    messages = [json.loads(frame[4:]) for frame in protocol.encode_request('same', {'a': 2})]
    for message in messages[:-1]:
        assert assembly.accept(message) is None
    with pytest.raises(protocol.ProtocolError, match='conflicting replay'):
        assembly.accept(messages[-1])
    assert assembly.reserved_bytes == 0


def test_decoder_stays_failed_after_bad_header():
    decoder = protocol.FrameDecoder()
    with pytest.raises(protocol.ProtocolError):
        decoder.feed(struct.pack('!I', 0))
    with pytest.raises(protocol.ProtocolError, match='closed'):
        decoder.feed(protocol.encode_request('valid', {})[0])


@pytest.mark.parametrize('wire', [b'{"value":NaN}', b'{"value":Infinity}', '{"value":1}'.encode('utf-16')])
def test_frames_refuse_nonstandard_json_and_encoding(wire):
    with pytest.raises(protocol.ProtocolError, match='frame JSON'):
        protocol.FrameDecoder().feed(struct.pack('!I', len(wire)) + wire)


def test_receiver_bounds_actual_framed_bytes_not_only_body_reservation():
    assembly = protocol.RequestAssembly()
    assembly.accept({'verb': 'BEGIN', 'id': 'a', 'total': 10})
    for _ in range(32):
        assembly.accept({'verb': 'CHUNK', 'id': 'a', 'offset': 0, 'data': '{}', 'padding': 'x' * 32000})
    with pytest.raises(protocol.ResourceExhausted, match='framed request budget'):
        assembly.accept({'verb': 'CHUNK', 'id': 'a', 'offset': 0, 'data': '{}', 'padding': 'x' * 32000})
    assert assembly.reserved_bytes == 0


def test_header_refuses_oversize_before_body_arrives():
    decoder = protocol.FrameDecoder()
    with pytest.raises(protocol.ProtocolError, match='frame limit'):
        decoder.feed(struct.pack('!I', protocol.MAX_FRAME + 1))


def test_request_overload_is_atomic_and_uses_actual_escaped_wire_size():
    with pytest.raises(protocol.ResourceExhausted):
        protocol.encode_request('large', {'sessions': ['\U0001f600' * 100000]})
    frames = protocol.encode_request('fits', {'sessions': ['x' * 70000]})
    messages = [json.loads(frame[4:]) for frame in frames]
    assembly = protocol.RequestAssembly()
    for message in messages[:-1]:
        assert assembly.accept(message) is None
    assert assembly.reserved_bytes > 0
    assert assembly.accept(messages[-1]) == ('fits', {'sessions': ['x' * 70000]})
    assert assembly.reserved_bytes == 0
