"""Authority-local subscription ordering over the existing bounded transports/replay."""
from copy import deepcopy
import logging
import threading
import uuid

from tui_gateway import event_replay
from tui_gateway.transport import FanoutTransport


def _utf16_len(text):
    return len(text.encode('utf-16-le', errors='surrogatepass')) // 2


def _utf16_tail(text, start):
    """``text`` from UTF-16 code unit ``start``, the unit ``stream_segments`` offsets count in."""
    return text.encode('utf-16-le', errors='surrogatepass')[2 * start:].decode('utf-16-le', errors='surrogatepass')


def _delta_stamp(payload):
    """``(turn_id, segment, offset)`` of a stamped ``message.delta``, or ``None`` when it carries no identity."""
    turn_id, segment, offset = payload.get('turn_id'), payload.get('segment'), payload.get('offset')
    if not (isinstance(turn_id, str) and turn_id):
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (segment, offset)):
        return None
    return turn_id, segment, offset


class SessionEvents:
    def __init__(self):
        self.lock = threading.RLock()
        self.fanout = FanoutTransport()
        # A private lifetime key prevents two profiles (or legacy TUI owners)
        # with the same persisted ID from reading each other's replay.
        self._key = uuid.uuid4().hex
        self.epoch = uuid.uuid4().hex
        self.sequence = 0
        self._public_id = None
        self.execution = {}
        # Synchronous same-thread recipients (API run projections): unlike fanout peers they
        # cannot lose a frame to a detach that races the writer thread.
        self.observers = set()
        # Set by the authority that attaches transports: retires the subscription of a
        # peer whose bounded backlog overflowed, so it cannot linger looking subscribed.
        self.on_overflow = None
        # The current assistant segment's cumulative streamed text, kept under ``lock`` as frames publish, so
        # a viewer attaching mid-stream snapshots what ring replay may no longer (or never) hold for it.
        self.live_text = ''
        # Where ``live_text`` sits in the turn's stamped reply (``tui_gateway/stream_segments.py``): ``turn_id``,
        # ``segment`` and the UTF-16 ``offset``/``end`` of that text inside the segment; ``None`` once an unstamped
        # delta fed it. A viewer merges the snapshot with stamped deltas it already received by offset, never by
        # comparing text: the first delta after ``last_sequence`` starts exactly at ``end``.
        self.live_span = None

    def live_inflight(self):
        """Caller holds ``lock``: the ``InflightTurn`` of the streaming segment, or ``None`` between segments."""
        if not self.live_text:
            return None
        inflight = {'assistant': self.live_text, 'streaming': True, 'user': '', 'segment_only': True}
        if self.live_span is not None:
            inflight['segment_stamp'] = dict(self.live_span)
        return inflight

    def _track_live_text(self, event_type, payload):
        if event_type == 'message.delta' and isinstance(payload, dict) and isinstance(payload.get('text'), str):
            self._track_live_delta(payload['text'], _delta_stamp(payload))
        elif event_type in ('message.start', 'message.complete', 'message.delta', 'message.interim', 'tool.start',
                            'tool.started'):
            # A new turn, a settled turn, a sealed segment (interim) or a boundary inside one (``text: None``, a
            # tool call): the text after it paints in a new place, so the snapshot restarts there.
            self.live_text, self.live_span = '', None

    def _track_live_delta(self, text, stamp):
        span = self.live_span
        if stamp is None or (span is None and self.live_text):
            # No identity to place this text by (or text before it had none): append, claim no span.
            self.live_text, self.live_span = self.live_text + text, None
            return
        turn_id, segment, offset = stamp
        end = offset + _utf16_len(text)
        if span is not None and span['turn_id'] == turn_id and segment < span['segment']:
            return  # a re-sent delta of a segment this turn already moved past
        if span is None or span['turn_id'] != turn_id or segment > span['segment'] or offset > span['end']:
            # The first text of a segment (or of the run after a boundary), or a gap: the span restarts at it.
            self.live_text = text
            self.live_span = {'turn_id': turn_id, 'segment': segment, 'offset': offset, 'end': end}
            return
        if end <= span['end']:
            return  # wholly re-sent: a replayed copy or a superseded attempt re-streaming what it already sent
        self.live_text += _utf16_tail(text, span['end'] - offset)
        span['end'] = end

    def watermark(self):
        with self.lock:
            if self.sequence and not event_replay.latest_seq(self._key):
                # The shared bounded cache evicted this session. Never reuse a
                # sequence in the old epoch, even if publication resumes later.
                self.epoch = uuid.uuid4().hex
                self.sequence = 0
            return self.epoch, self.sequence

    def publish(self, session_id, payload, *, event_type="message.complete", execution=None):
        # ``execution`` stamps this frame alone; the shared stamp is read without the lock elsewhere.
        with self.lock:
            self.watermark()
            frame = {'jsonrpc': '2.0', 'method': 'event', 'params': {
                **(self.execution if execution is None else execution), 'type': event_type, 'session_id': self._key,
                'payload': deepcopy(payload), 'replay_epoch': self.epoch}}
            event_replay._stamp_event(frame)
            if frame['params']['seq'] <= self.sequence:
                # Another publisher may evict us after watermark's lookup.
                self.epoch = uuid.uuid4().hex
                frame['params']['replay_epoch'] = self.epoch
            # The ring froze the frame under its private lookup key; live recipients see the
            # canonical ID here and ``since`` restores it on the thawed replay copies.
            frame['params']['session_id'] = session_id
            self._public_id = session_id
            self.sequence = frame['params']['seq']
            self._track_live_text(event_type, payload)

            def overflow(transport):
                # The peer's socket is healthy; only its event subscription ended. Same
                # shape as the client's own synthetic gap so one consumer handles both.
                if self.on_overflow is not None:
                    self.on_overflow(transport)
                return {'jsonrpc': '2.0', 'method': 'event', 'params': {
                    'type': 'session.replay_gap', 'session_id': session_id,
                    'payload': {'replay_epoch': self.epoch, 'latest_seq': self.sequence}}}
            delivered = self.fanout.write(frame, overflow=overflow)
            for observer in tuple(self.observers):
                try:
                    observer(frame)
                    delivered = True
                except Exception:
                    # A request-owned projection cannot undo committed publication or starve peers.
                    logging.getLogger(__name__).warning('Session event observer failed for %s', session_id,
                                                        exc_info=True)
            # True when a live viewer or observer took the frame; the replay ring alone is not
            # delivery (a detached finite viewer reads nothing until it reattaches).
            return delivered

    def catch_up(self, transport, epoch, sequence):
        """Caller holds ``lock``, before *transport* joins ``fanout``: write it the frames after the
        cursor its connection's replay answered. A reconnect asks ``session.events.since`` and then
        ``session.resume``; whatever the session published in between went to no live peer of this
        socket, and the replay was already over, so a reply's tail and ``message.complete`` were lost
        and the view stayed on its first chunk. Under the lock nothing can publish ahead of them."""
        replay = self.since(epoch, sequence)
        if replay['snapshot_required']:
            return
        for frame in replay['events']:
            transport.write({'jsonrpc': '2.0', 'method': 'event', 'params': frame})

    def since(self, epoch, sequence):
        with self.lock:
            current_epoch, latest = self.watermark()
            frames = event_replay.events_since(self._key, sequence)
            missing = (epoch != current_epoch or sequence > latest
                       or event_replay.is_truncated(self._key, sequence)
                       or event_replay.latest_seq(self._key) != latest)
            events = [] if missing else [
                {**frame, 'session_id': self._public_id} if frame.get('session_id') == self._key else frame
                for frame in frames]
            return {'events': events, 'latest_seq': latest, 'last_sequence': latest,
                    'epoch': current_epoch, 'replay_epoch': current_epoch,
                    'truncated': missing, 'snapshot_required': missing, 'count': len(events)}
