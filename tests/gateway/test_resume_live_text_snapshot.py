"""A fresh viewer attaching mid-stream gets the segment's whole streamed text in ``session.resume.inflight``
(head published before it detached, tail published while it was away), then only later deltas."""
import threading

import pytest

from gateway.session_controls import AuthorityConnection
from tests.gateway.test_resume_omit_messages import _local_session, _resume


async def _resume_keeping_transport(authority, actor, ref, **params):
    conn = AuthorityConnection(authority, object(), {'user_id': 'owner'})
    conn.actor = actor
    return await conn.resume(ref, {'session_id': ref.session_id, **params})


def _publish(authority, ref, event_type, payload):
    live = authority.sessions[ref.session_id]
    with live.event_stream.lock:
        live.event_stream.publish(ref.session_id, payload, event_type=event_type)


def _delta(authority, ref, text):
    _publish(authority, ref, 'message.delta', {'text': text})


@pytest.mark.asyncio
@pytest.mark.parametrize('omit', [False, True])
async def test_reattach_gets_head_and_tail_then_future_deltas_once(tmp_path, monkeypatch, omit):
    authority, actor, ref, _ = await _local_session(tmp_path, monkeypatch)
    _publish(authority, ref, 'message.start', {})
    _delta(authority, ref, 'HEAD ')
    first = await _resume(authority, actor, ref, omit_messages=omit)
    assert first['inflight'] == {'assistant': 'HEAD ', 'streaming': True, 'user': '', 'segment_only': True}
    await authority.detach(actor, first['subscription_id'])
    _delta(authority, ref, 'TAIL ')  # published while nobody was attached
    events = authority.sessions[ref.session_id].event_stream
    tail_seq = events.watermark()[1]
    seen, got_next = [], threading.Event()  # the fanout writes from its own daemon thread

    def write(frame):
        seen.append(frame)
        if frame['params']['payload'].get('text') == 'NEXT':
            got_next.set()
        return True
    authority.events[actor.transport_id] = type('T', (), {'write': staticmethod(write),
                                                          'close': staticmethod(lambda: None)})()
    second = await _resume_keeping_transport(authority, actor, ref, omit_messages=omit)
    assert second['inflight']['assistant'] == 'HEAD TAIL '
    # The snapshot covers TAIL. Not pinned to the stream's head: an inventory past the reply budget
    # (``REPLY_INVENTORY_BUDGET_S``, easily missed on a throttled runner) publishes a late ``session.info``
    # that can land after the snapshot, while the resume is still building its reply.
    assert tail_seq <= second['last_sequence'] <= events.watermark()[1]
    _delta(authority, ref, 'NEXT')
    got_next.wait(30)
    assert got_next.is_set()
    deltas = [f['params']['payload']['text'] for f in seen if f['params']['type'] == 'message.delta']
    assert deltas == ['NEXT']  # the snapshot covers HEAD+TAIL; later frames arrive exactly once


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', [('message.delta', {'text': None}), ('message.complete', {'text': 'x'}),
                                      ('message.start', {}), ('tool.start', {'name': 't'})])
async def test_settled_or_new_segments_are_never_resurrected(tmp_path, monkeypatch, boundary):
    authority, actor, ref, _ = await _local_session(tmp_path, monkeypatch)
    _publish(authority, ref, 'message.start', {})
    _delta(authority, ref, 'OLD')
    _publish(authority, ref, *boundary)
    assert (await _resume(authority, actor, ref))['inflight'] is None
    _delta(authority, ref, 'NEW')
    assert (await _resume(authority, actor, ref))['inflight']['assistant'] == 'NEW'


def test_live_text_is_per_session_stream():
    from gateway.session_events import SessionEvents
    a, b = SessionEvents(), SessionEvents()
    a.publish('a', {'text': 'SECRET_A'}, event_type='message.delta')
    assert a.live_inflight()['assistant'] == 'SECRET_A'
    assert b.live_inflight() is None


def _stamped(events, segments, event_type, payload):
    """Publish a frame stamped by the same rule ``TurnRunner._stamp_segment`` applies."""
    from tui_gateway.stream_segments import stamp_reply_frame
    events.publish('s', stamp_reply_frame(segments, event_type, payload), event_type=event_type)


@pytest.mark.asyncio
async def test_resume_snapshot_names_the_utf16_span_of_stamped_deltas(tmp_path, monkeypatch):
    from tui_gateway.stream_segments import TurnSegments
    authority, actor, ref, _ = await _local_session(tmp_path, monkeypatch)
    events, segments = authority.sessions[ref.session_id].event_stream, TurnSegments()
    with events.lock:
        _stamped(events, segments, 'message.start', {})
        _stamped(events, segments, 'message.delta', {'text': 'A2 😀 '})  # the emoji is 2 UTF-16 units
        _stamped(events, segments, 'message.delta', {'text': 'TAIL '})
    inflight = (await _resume(authority, actor, ref))['inflight']
    assert inflight['assistant'] == 'A2 😀 TAIL '
    assert inflight['segment_stamp'] == {'turn_id': segments.turn_id, 'segment': 0, 'offset': 0, 'end': 11}
    with events.lock:  # the next delta a viewer receives starts exactly where the snapshot ends
        _stamped(events, segments, 'message.delta', {'text': 'NEXT'})
    assert segments.offset == 15


def test_resent_overlapping_delta_is_not_counted_twice():
    from gateway.session_events import SessionEvents
    events = SessionEvents()
    turn = {'turn_id': 't1', 'segment': 0}
    for offset, text in ((0, 'Hello '), (0, 'Hello '), (3, 'lo wor'), (6, 'wor'), (9, 'ld')):
        events.publish('s', {'text': text, **turn, 'offset': offset}, event_type='message.delta')
    assert events.live_inflight()['assistant'] == 'Hello world'
    assert events.live_inflight()['segment_stamp'] == {**turn, 'offset': 0, 'end': 11}


@pytest.mark.parametrize('boundary', ['message.interim', 'tool.start'])
def test_a_boundary_restarts_the_span_at_the_next_stamped_delta(boundary):
    from gateway.session_events import SessionEvents
    from tui_gateway.stream_segments import TurnSegments
    events, segments = SessionEvents(), TurnSegments()
    _stamped(events, segments, 'message.start', {})
    _stamped(events, segments, 'message.delta', {'text': 'before '})
    _stamped(events, segments, boundary, {'text': 'before', 'name': 't'})
    assert events.live_inflight() is None
    _stamped(events, segments, 'message.delta', {'text': 'after'})
    sealed = boundary == 'message.interim'  # an interim seals the segment; a tool call keeps counting in it
    assert events.live_inflight()['assistant'] == 'after'
    assert events.live_inflight()['segment_stamp'] == {
        'turn_id': segments.turn_id, 'segment': 1 if sealed else 0, 'offset': 0 if sealed else 7,
        'end': 5 if sealed else 12}


def test_unstamped_deltas_claim_no_identity():
    from gateway.session_events import SessionEvents
    events = SessionEvents()
    events.publish('s', {'text': 'legacy'}, event_type='message.delta')
    assert events.live_inflight() == {'assistant': 'legacy', 'streaming': True, 'user': '', 'segment_only': True}


def test_a_stale_segment_or_a_gap_never_widens_the_claimed_span():
    from gateway.session_events import SessionEvents
    events = SessionEvents()
    for segment, offset, text in ((1, 0, 'new '), (0, 4, 'old'), (1, 9, 'after a gap')):
        events.publish('s', {'text': text, 'turn_id': 't1', 'segment': segment, 'offset': offset},
                       event_type='message.delta')
        if offset == 4:  # a re-sent delta of the sealed segment 0 is not this segment's text
            assert events.live_inflight()['assistant'] == 'new '
    # Frames 4..9 never published here: the span restarts at the gap instead of claiming text it lacks.
    assert events.live_inflight()['assistant'] == 'after a gap'
    assert events.live_inflight()['segment_stamp'] == {'turn_id': 't1', 'segment': 1, 'offset': 9, 'end': 20}


def test_stamped_text_after_unstamped_text_claims_no_span():
    from gateway.session_events import SessionEvents
    events = SessionEvents()
    events.publish('s', {'text': 'legacy '}, event_type='message.delta')
    events.publish('s', {'text': 'stamped', 'turn_id': 't1', 'segment': 0, 'offset': 0}, event_type='message.delta')
    assert events.live_inflight() == {'assistant': 'legacy stamped', 'streaming': True, 'user': '',
                                      'segment_only': True}
