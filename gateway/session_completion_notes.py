"""A completion for a local session no viewer is watching is written into it as a note (#134889 LC-7).

Main's CLI and TUI hosted async delegations and background processes in their own process, so once
the user left nothing started a turn for them. Under the gateway the owner outlives its viewers. The
completion is admitted like any producer input. Producers still coalesce a batch into one admission
under its primary identity, the siblings' identities in its descriptor: a lookup by any of them finds
it (``completion_admission``), results already admitted are left out of a new batch, and the one
outcome, note or turn, covers the whole batch. When that admission comes up for execution with no
viewer attached (the authority's own subscriber records) the executor records it in the session's
history, where the next turn sees it, and settles the admission without a model call. Queue order, restart survival and
exactly-once are the admission contract's. With a viewer attached it runs as the turn it always was,
except a local cron result, which is always a note; messaging, Kanban, ``/loop`` and heartbeat wakes
never reach here.

A result the parent carries on from (``resumes_parent``: an async delegation's final result, a
background process's exit) runs as a turn with no viewer too. A viewer going away is not the user
ending the work: a laptop asleep, a dropped socket or a closed browser tab leaves the session's plan
mid-flight, and the parent dispatched that work to carry on when it finished. Only a chat the user
closed (exit, /new, a resume or branch switch) keeps its results as notes. An async delegation whose
final result has a failed child (``wakes_parent``) is a resume too, bounded: at most
``UNATTENDED_WAKE_LIMIT`` failed-result wakes in a row while nobody watches. Only failed delegations
spend that limit: successful results and process exits, whatever their exit code, are not counted.
"""
import json
from functools import partial

# ``_completion_delivery_identity`` kinds: an async delegation's result, a background process's exit
# and its watch-pattern notices.
_NOTED_PRODUCERS = frozenset({'async_delegation', 'completion', 'watch_match', 'watch_disabled'})
# A cron result for a local chat (``session_cron.deliver_to_local_chat``) is finished text: a note
# with or without a viewer, since a turn over it would only spend a model call (#143547 CRON-11).
_ALWAYS_NOTED = frozenset({'cron'})
# Failed-delegation wakes one session may run in a row with nobody watching. A woken parent that
# re-dispatches into the same failure (billing, a usage limit) would otherwise wake itself forever; past
# the limit the failure is a note again until a viewer attaches or the user sends something.
UNATTENDED_WAKE_LIMIT = 3
# Backoff while another turn holds the conversation's lease (``_write_note``); the warning repeats
# at most this often, so a long turn ahead of the note is visible without flooding the log.
_LEASE_RETRY_MIN_S = 0.25
_LEASE_RETRY_MAX_S = 2.0
_LEASE_NOTICE_EVERY_S = 60.0


def _producer(identity):
    try:
        return json.loads(identity)[0]
    except (ValueError, TypeError, IndexError, KeyError):
        return None


def noted_completion(row):
    """*row* is a local completion admission: a note when it runs with no viewer attached (unless it
    wakes its parent, which ``note_if_unviewed`` decides when it runs)."""
    descriptor = (row or {}).get('payload', {}).get('local_automation_v1')
    return descriptor is not None and _producer(descriptor['identity']) in _NOTED_PRODUCERS | _ALWAYS_NOTED


def _closed_by_user(authority, session_id):
    """The user ended this chat's work: exit or /new refused its dispatches (``refuse_for_session``,
    held in memory), its close is still pending in ``pending_closes`` (re-armed from the store after a
    restart, while its FIFO is busy), or its row ended for a reason recovery does not resume (which
    outlives a restart). An orphan reap (the last viewer vanished) and an agent close are not the user's.
    A wake in a closed chat would also run its re-dispatched child inside the wake turn, refused the
    background."""
    from tools.async_delegation import refused_for_session
    from gateway.session_viewer_close import ORPHAN_REASONS, _physical_target
    # A close still pending (its FIFO was busy) outlives a restart in ``pending_closes``; the refusal does not.
    pending = authority.pending_closes.get(session_id)
    if refused_for_session((authority.profile_id, session_id)) or (
            pending is not None and pending.reason not in ORPHAN_REASONS):
        return True
    from hermes_state_common import _RECOVERABLE_END_REASONS
    row = authority.db.get_session(_physical_target(authority, session_id)) or {}
    return bool(row.get('ended_at')) and row.get('end_reason') not in _RECOVERABLE_END_REASONS


def _wakes_unviewed_parent(authority, session_id, live, descriptor, *, consume=True):
    """A result the parent carries on from (``resumes_parent``) or a failed async delegation
    (``wakes_parent``) with no viewer attached: the parent dispatched that work and is waiting on it,
    so it runs as the parent's turn instead of stalling until a human returns. Not in a chat the user
    closed (``_closed_by_user``); a failed delegation not past ``UNATTENDED_WAKE_LIMIT`` (a process
    exit is a resume whatever its exit code)."""
    failed = descriptor.get('wakes_parent')
    if not (failed or descriptor.get('resumes_parent')):
        return False
    closed = _closed_by_user(authority, session_id)
    if not (failed or closed):
        return True
    if closed or live.unattended_wakes >= UNATTENDED_WAKE_LIMIT:
        if failed and consume:
            import logging
            logging.getLogger(__name__).warning(
                'parent %s not woken: failed delegation result kept as a note (%s)', session_id,
                'its chat was closed' if closed else f'{UNATTENDED_WAKE_LIMIT} unattended wakes in a row')
        return False
    if consume:
        live.unattended_wakes += 1
    return True


def completion_is_note(authority, ref, row, *, consume=False):
    """Share the claim's note decision with fold scans without spending a wake on a read.

    Only the row's own claim consumes its failed-result wake allowance.
    """
    live = authority.sessions.get(ref.session_id)
    if not noted_completion(row) or live is None:
        return False
    descriptor = row['payload']['local_automation_v1']
    if live.subscribers and _producer(descriptor['identity']) not in _ALWAYS_NOTED:
        return False
    return not _wakes_unviewed_parent(authority, ref.session_id, live, descriptor, consume=consume)


async def reopen_for_turn(authority, ref):
    """A turn (never a note) makes *ref*'s finalized row live again: a local session's receipt tip, or
    the row of a conversation its chat moved past (gateway/session_detached.py), which the chat's /new
    ended."""
    from gateway.session_runtime_workers import tracked_write
    live = authority.sessions.get(ref.session_id)
    if live is not None and live.detached:
        from gateway.session_api import reopen_api_session as reopen
    else:
        from gateway.session_local_recovery import reopen_local_session as reopen
    await tracked_write(authority, partial(reopen, authority, ref))


async def note_if_unviewed(authority, ref, row):
    """The claimed *row*'s response when it was written as a note instead of running, else None.
    It is the FIFO head, so the note lands after every input committed before it and before every
    input committed after; the viewer check and the decision share one loop step."""
    live = authority.sessions.get(ref.session_id)
    if live is not None and (live.subscribers or 'local_automation_v1' not in row['payload']):
        live.unattended_wakes = 0  # someone is watching, or the user sent this: recoveries count afresh
    if not noted_completion(row):
        return None
    from gateway.session_runtime_workers import tracked_write
    descriptor = row['payload']['local_automation_v1']
    if not completion_is_note(authority, ref, row, consume=True):
        # The claim left a closed row closed for a note; the turn reopens it as any first turn does.
        await reopen_for_turn(authority, ref)
        return None
    metadata = note_metadata(descriptor)
    if descriptor.get('requested_stop'):
        # Unfinished work someone stopped, and nobody is watching to see it: the parent stays asleep on it.
        import logging
        logging.getLogger(__name__).warning(
            'parent %s not woken: delegation %s stopped on request (%s), kept as a note (no viewer attached)',
            ref.session_id, descriptor['identity'], ','.join(descriptor['requested_stop']))
    # Keyed by its admission: a claim is never replayed, and this keeps a write retry exact.
    target = await _write_note(authority, ref, lambda target: tracked_write(authority, partial(
        authority.db.append_completion_note, target, row['admission_id'], row['payload']['text'],
        descriptor.get('display_kind', 'internal_notification'), metadata)))
    refresh = getattr(authority.runner, '_refresh_agent_cache_message_count', None)
    if refresh is not None:  # the owner's own row: a cached agent stays, as after a turn's writes
        await refresh(live.route, target)
    _refresh_outcome_after_note(authority.db, target)
    authority.pending_results[row['admission_id']] = {'result': {'final_response': '', 'messages': []}, 'usage': {}}
    return ''


def note_metadata(descriptor):
    """A completion note's display metadata: every identity it covers and the producer's card fields."""
    metadata = {'completion_note': sorted(set(descriptor.get('identities') or ()) | {descriptor['identity']})}
    metadata.update({key: descriptor[key] for key in ('display_text', 'notification_category') if key in descriptor})
    return metadata


async def _write_note(authority, ref, write):
    """Run ``write(target)`` against the session's physical tip; return that target.

    The note refuses a transcript another turn on the same conversation holds the turn lease of
    (``SessionTurnLeaseLostError``): another process's turn, or a sibling session's. That refusal is
    transient (the lease ends with its turn, or goes stale and the guard reclaims it), while failing
    the admission loses the completion for good: its producer was acknowledged at admission and its
    request id is admitted once, so a re-offer resolves to the failed row (deleg_e757744d). The FIFO
    head waits for the lease instead, as a turn of this session waits for it, and writes then; the
    note is keyed by its admission, so a refused try leaves nothing to repeat. A drain that stops
    meanwhile cancels the wait, as it does a running turn. The tip is re-read each try: a
    compression may rotate it while we wait."""
    import asyncio
    import logging

    from gateway.session_viewer_close import _physical_target
    from hermes_state_errors import SessionTurnLeaseLostError
    delay, waited, notice_at = 0.0, 0.0, 0.0
    while True:
        target = _physical_target(authority, ref.session_id)
        try:
            await write(target)
            return target
        except SessionTurnLeaseLostError:
            if waited >= notice_at:
                logging.getLogger(__name__).warning(
                    'Completion note for %s waits for another turn\'s lease on %s (%.0fs so far)',
                    ref.session_id, target, waited)
                notice_at = waited + _LEASE_NOTICE_EVERY_S
        delay = min(max(delay * 2, _LEASE_RETRY_MIN_S), _LEASE_RETRY_MAX_S)
        await asyncio.sleep(delay)
        waited += delay


def _refresh_outcome_after_note(db, target):
    """[daveey] A note runs no turn, so no turn-end refresh restores the chat's stored outcome: refresh it here, off
    the admission loop (the judge may call a model), as ``gateway/turn_outcome.py`` does for a turn. Best-effort."""
    from agent.memory_provider import spawn_context_thread
    from hermes_state_outcome import refresh_outcomes_quietly
    try:
        thread = spawn_context_thread(refresh_outcomes_quietly, name='completion-note-outcome',
                                      args=([target], 'notice'), kwargs={'db': db})
        thread.start()
        return thread
    except Exception:  # health: allow BLE001 -- a thread that can't start skips one refresh; the next refresh heals
        import logging
        logging.getLogger(__name__).warning('completion-note outcome refresh did not start for %s', target,
                                            exc_info=True)
        return None
