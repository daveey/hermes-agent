"""Whether a background process will still report back to the agent that spawned it.

The session reapers keep an owner alive while this is true: the notice reaches the model only through that owner's
notification poller, and closing the owner kills the process (``agent_close``).

Pending is read from live state, never estimated with a timer. A notice is pending from the moment its process can
produce it until a consumer has finished routing it:

1. a notifying process is still running;
2. its exit is recorded but the completion is not yet published (``_completion_event`` unset; the registry keeps
   such entries out of pruning; the claim already snapshotted the event, ``unpublished_completions``);
3. the event sits in ``completion_queue``;
4. a consumer has taken it off the queue and is still routing it (an in-flight hold, taken atomically with the
   dequeue under the queue's own lock and released only after routing returns).

Routing ends by admitting a turn (``running`` then protects the session), requeueing (state 3 again) or dropping the
notice, so every state hands over to the next without a gap and none outlives delivery.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import threading

logger = logging.getLogger("tools.process_registry")

# Owner task id → number of dequeued-but-unrouted events. take_ready increments it under the queue's mutex in the same
# critical section as the dequeue, and awaits_notification reads queue and counter under that mutex too, so a reader
# sees each event either queued or held, never neither. Decrements (release) happen only after any requeue.
_in_flight: collections.Counter = collections.Counter()
_in_flight_lock = threading.Lock()  # always taken after the queue mutex, never before
# The dequeued-but-unrouted events themselves (same custody as ``_in_flight``), so an orderly backend exit can
# journal a notice a poller holds mid-routing (``pending_events``) instead of only knowing its owner is busy.
_in_flight_events: list = []


def _owner(evt) -> str:
    return str(evt.get("owner_task_id") or "") if isinstance(evt, dict) else ""


def _adjust(owners, delta: int) -> None:
    with _in_flight_lock:
        for owner in owners:
            if not owner:
                continue
            _in_flight[owner] += delta
            if _in_flight[owner] <= 0:
                del _in_flight[owner]


def _hold_events(events) -> None:
    with _in_flight_lock:
        _in_flight_events.extend(events)


def _release_events(events) -> None:
    with _in_flight_lock:
        for evt in events:
            for index, held in enumerate(_in_flight_events):
                if held is evt:
                    del _in_flight_events[index]
                    break


def take_ready(queue, timeout: float, *, wake_generation=None) -> list:
    """Dequeue everything queued (waiting up to ``timeout`` for the first event) and hold each event's owner in the
    same critical section, so no reaper check can observe an event as neither queued nor held. Delegation result
    copies are marked held there too (``async_delegation.mark_copy_held``), so another session's orphan sweep never
    re-offers a copy this poller is still routing. The caller MUST pass the result to ``release`` once routing
    returns."""
    from tools.async_delegation import completion_copy_id, mark_copy_held
    with queue.not_empty:
        unchanged = wake_generation is None or wake_generation == getattr(queue, "_notification_generation", 0)
        if not queue._qsize() and timeout > 0 and unchanged:
            queue.not_empty.wait(timeout)
        events = list(queue.queue)
        queue.queue.clear()
        if events:
            queue.not_full.notify_all()
            _adjust((_owner(e) for e in events), +1)
            _hold_events(events)
            for evt in events:
                mark_copy_held(completion_copy_id(evt))
    return events


def hold(events) -> None:
    """Take the in-flight hold for events a consumer removed from somewhere other than the queue (a session's held
    list). Call it inside the critical section that removes them, so they are never in neither place; ``release``
    once routing returns. Like ``take_ready`` it takes its own held-copy mark per delegation result, which
    ``release`` ends (a mark the source list held stays the source's to end)."""
    if events:
        from tools.async_delegation import completion_copy_id, mark_copy_held
        _adjust((_owner(e) for e in events), +1)
        _hold_events(events)
        for evt in events:
            mark_copy_held(completion_copy_id(evt))


def release(events) -> None:
    """End the in-flight hold ``take_ready`` took for ``events`` (after any requeue, so coverage is continuous)."""
    if events:
        from tools.async_delegation import completion_copy_id, unmark_copy_held
        _adjust((_owner(e) for e in events), -1)
        _release_events(events)
        for evt in events:
            unmark_copy_held(completion_copy_id(evt))


def pending_events(queue) -> list:
    """Every event still owed to a consumer: queued, or dequeued and still being routed. Read under the queue mutex
    with the in-flight hold, like ``awaits_notification``, so an event moving between the two is seen at least once
    (callers dedupe by process id). Historical result files are never included."""
    with queue.mutex:
        with _in_flight_lock:
            return [*queue.queue, *_in_flight_events]


def unpublished_completions(registry) -> list:
    """[daveey] Completion events whose finish is claimed but not yet published to ``completion_queue`` (state 2):
    the unredacted event the claiming ``_move_to_finished`` snapshotted under ``registry._lock`` (copies; the
    caller redacts). Read this BEFORE
    ``pending_events``: publication queues the event before it sets ``_completion_event``, so an event is seen
    here, or already in the queue by the time that is read. Re-adopt-sink jobs are excluded (they never reach
    the queue)."""
    with registry._lock:
        return [dict(s._completion_notice) for store in (registry._running, registry._finished) for s in store.values()
                if s.notify_on_complete and retained_for_delivery(s)
                and not getattr(s, "_completion_via_readopt_sink", False)
                and isinstance(getattr(s, "_completion_notice", None), dict)]


@contextlib.contextmanager
def drained_for(registry, owns):
    """Dequeue everything queued for a caller that consumes only the events ``owns`` accepts (the TUI's post-turn
    safety net). Every dequeued event's owner is held — foreign ones too — until the rejected events are back on
    the queue and the accepted ones (yielded) have been routed, so no owner is ever between queue and hold."""
    registry.restore_completions()
    ready = take_ready(registry.completion_queue, 0)
    try:
        mine = [evt for evt in ready if owns(evt)]
        for evt in ready:
            if not any(evt is m for m in mine):
                registry.completion_queue.put(evt)
        yield mine
    finally:
        release(ready)


def retained_for_delivery(s) -> bool:
    """A finished entry whose completion is not yet published: pruning it would erase the only record that a notice
    is coming, so the registry keeps it until publication."""
    return bool(getattr(s, "_finish_claimed", False)) and not s._completion_event.is_set()


def _notifies(s) -> bool:
    return bool(s.notify_on_complete or (s.watch_patterns and not s._watch_disabled))


def awaits_notification(registry, owner_task_ids) -> bool:
    """Whether a process spawned by one of ``owner_task_ids`` still has a notice on its way. Keyed on the RAW
    spawning owner, the same field agent close kills by, so it survives compression rotating the session key."""
    owners = {o for o in owner_task_ids if o}
    if not owners:
        return False
    # (1) Running. _any_running refreshes checkpoint-recovered entries (outside the registry lock) first, so a
    # recovered PID that died unobserved reads as gone instead of pinning its owner.
    if registry._any_running(lambda s: s.owner_task_id in owners and _notifies(s)):
        return True
    with registry._lock:
        # (2) Exit recorded, completion not yet published. Entries closed without a collected exit (recovered PID
        # gone or reused) set _completion_event too, so they never count.
        if any(s.owner_task_id in owners and s.notify_on_complete and s.exited and not s._completion_event.is_set()
               for store in (registry._running, registry._finished) for s in store.values()):
            return True
    queue = registry.completion_queue
    with queue.mutex:  # (3) queued and (4) in flight, read together: take_ready moves events between them here
        with _in_flight_lock:
            if any(_in_flight.get(o) for o in owners):
                return True
        return any(_owner(evt) in owners for evt in queue.queue)


def completion_event(session) -> dict:
    """The completion event a finished notifying session publishes (exit fields and output as recorded now),
    unredacted: redaction runs hooks and may look the session up in the registry, so it must happen outside
    ``registry._lock`` (``publishable``)."""
    from tools.process_registry import ProcessRegistry, _completion_output
    return {
        "type": "completion",
        "session_id": session.id,
        "session_key": session.session_key,
        "task_id": session.task_id,
        "owner_task_id": session.owner_task_id,
        "command": session.command,
        **({"handoff_note": session.handoff_note} if session.handoff_note else {}),
        **ProcessRegistry._exit_fields(session),
        # A consumer that relays the output (a bot DM's reply) must know it is not whole.
        **_completion_output(session),
        # Stable producer identity across checkpoint recovery (unlike a consumer-observed completion timestamp).
        "started_at": session.started_at,
    }


def publishable(event: dict) -> dict:
    """A redacted copy of a claim's event (the claim's own snapshot stays unchanged for concurrent readers)."""
    from tools.process_registry import _redact_process_result
    event = dict(event)
    _redact_process_result(event)
    return event


def claim_completion_event(session) -> None:
    """[daveey] Called by the claiming ``_move_to_finished`` under ``registry._lock``: snapshot the event it will
    publish, so an orderly backend exit reads the real event while the claim is unpublished
    (``unpublished_completions``; its reader redacts) instead of waiting for the publisher. Never raises (the
    claim must not be left half-done); on failure publication builds the event itself."""
    try:
        session._completion_notice = completion_event(session)
    except Exception:
        logger.warning("completion event for %s not built at its claim", session.id, exc_info=True)
