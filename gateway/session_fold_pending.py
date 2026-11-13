"""Notices already pending at a turn boundary ride the turn that starts there. [daveey]

A busy chat ran one turn per queued row: a process completion admitted mid-turn waited behind every
row ahead of it (an update-resume continuation, a Kanban batch), one full turn each. On 2026-11-13
proc_467899b1fa1b's completion reached chat 20261026_235722_bef6c6 59 min after admission: three
update-resume turns and one Kanban batch each started without it (t_01a1d663082aa53a). The
admission-time folds (``session_automation._admit_folded_completion``, ``kanban_wake_fold``) only join
a row of the same kind at the queue tail, and an update-resume head is no queued row at all.

So when an automation turn (or any update-resume continuation) starts, every completion and Kanban wake
still queued behind it, up to the first human input, is handed to that claim and appended to its text,
each with its own envelope text intact. Custody is the queued-steer transfer
(``hermes_state_runtime.steer_session_input``): each row goes queued -> terminal ``steering`` bound to
the claim's generation and settles in the claim's own settlement write. It reads delivered only when the
claim completed (``steered``, acked once); any other ending (failed, interrupted, unknown, a pause for an
update, an unstarted requeue) puts it back in its own FIFO place (``_requeue_transfer``), so nothing is
lost and nothing settles twice. The queue's own fold rules and the rows themselves are unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging
from functools import partial

from hermes_state_runtime import RuntimeStoreError, list_session_admissions

logger = logging.getLogger(__name__)

# The producers whose queued rows join a starting turn: async results and Kanban card events.
FOLDABLE_PRODUCERS = frozenset({'completion', 'async_delegation', 'kanban-wake'})
# A turn that runs as one input of its own: never extended.
_WHOLE_TURN_KEYS = frozenset({'finite', 'api_turn_v1', 'cli_turn_v1'})


def _automation(payload):
    return payload.get('local_automation_v1') or (payload.get('native_text_v1') or {}).get('automation') or {}


def _producer(row):
    try:
        identity = json.loads(row.get('request_id') or '')
    except (TypeError, ValueError):
        return None
    return identity[0] if isinstance(identity, list) and identity else None


def accepts_fold(row):
    """The head extends: an update-resume continuation, or a completion / Kanban wake turn itself. A person's
    own message stays as typed (it is the bubble they see), finite/API/CLI turns run their one input only, and
    a liveness or loop tick keeps its own short turn (its process's later ticks fold into it at admission)."""
    payload = row.get('payload') or {}
    if _WHOLE_TURN_KEYS & set(payload) or not isinstance(payload.get('text'), str):
        return False
    return bool(row.get('update_resume')) or (bool(_automation(payload)) and _producer(row) in FOLDABLE_PRODUCERS)


def _foldable(row):
    payload = row['payload']
    descriptor = _automation(payload)
    if not descriptor or _producer(row) not in FOLDABLE_PRODUCERS or not isinstance(payload.get('text'), str):
        return False
    # A remote Kanban notice is acked by the keys its own turn's user row carries: it keeps its own turn.
    return not descriptor.get('kanban_event_keys')


def candidates(authority, ref, head):
    """Queued rows behind ``head`` that may join it, in admission order. A held notifier wake and other
    producer rows (liveness ticks, loop ticks) are passed over; the first human input ends the scan, so a
    notice never jumps a prompt queued before it."""
    from gateway.session_notifier_drain import eligible
    picked = []
    for row in list_session_admissions(authority.db, session_id=ref.session_id):
        if row['status'] != 'queued' or row['admission_id'] == head['admission_id']:
            continue
        if not _automation(row['payload']):
            break
        if eligible(row) and _foldable(row):
            picked.append(row)
    return picked


async def _check(authority, ref, live, row):
    """The checks the row's own claim would run (route, owner, identity), read-only."""
    if 'local_automation_v1' in row['payload']:
        from gateway.session_automation import check_local_automation
        check_local_automation(authority, ref, row)
        return
    from gateway.session_envelope import check_native_route
    await check_native_route(authority.runner, row['payload'], authority.physical_target(ref), live.source,
                             authority.runner._adapter_for_source(live.source))


def _refresh(payload):
    from gateway.kanban_wake_fold import refresh_claimed
    from hermes_cli.kanban_stale_ask import refresh_payload
    refresh_claimed(payload)
    refresh_payload(payload)


def folded(authority):
    """session -> (generation, {admission_id}) this module handed to that claim."""
    return vars(authority).setdefault('folded_transfers', {})


async def fold_pending(authority, ref, live, row, event):
    """Hand every pending notice behind the claimed ``row`` to it and append each one's text to ``event``.
    Returns the folded admission ids. A row refused by its checks or by the transfer write stays queued."""
    if not accepts_fold(row) or not isinstance(event.text, str):
        return []
    from gateway.session_queued_controls import _register, _unregister
    from gateway.session_runtime_workers import tracked_write
    from hermes_state_runtime import steer_session_input
    generation, sid, taken, texts = row['generation'], ref.session_id, [], []
    for candidate in await asyncio.to_thread(candidates, authority, ref, row):
        try:
            await _check(authority, ref, live, candidate)
        except RuntimeStoreError:
            continue  # its own claim refuses it in FIFO order
        payload = dict(candidate['payload'])
        await asyncio.to_thread(_refresh, payload)
        with live.event_stream.lock:
            _register(authority, sid, generation, candidate['admission_id'])
        try:
            _committed, fresh = await tracked_write(authority, partial(
                steer_session_input, authority.db, epoch=authority.epoch,
                admission_id=candidate['admission_id'], generation=generation))
        except RuntimeStoreError:
            with live.event_stream.lock:
                _unregister(authority, sid, generation, candidate['admission_id'])
            continue
        if not fresh:
            continue
        taken.append(candidate['admission_id'])
        texts.append(payload['text'].strip())
    if not taken:
        return []
    folded(authority)[sid] = (generation, set(taken))
    event.text = '\n\n'.join([event.text.rstrip(), *texts])
    logger.info('Folded %d pending admission(s) into turn: session=%s admission=%s generation=%s folded=%s',
                len(taken), sid, row['admission_id'], generation, ','.join(taken))
    authority._publish_pending(ref)
    return taken


def settle_outcome(authority, row, transfers, outcome):
    """``transfers`` for the claim's settlement write: a folded row reads delivered only when the claim
    completed; under any other outcome it goes back to its own FIFO place."""
    entry = folded(authority).get(row['target_session_id'])
    if entry is None or entry[0] != row['generation']:
        return transfers
    transfers = dict(transfers or {})
    for admission_id in entry[1]:
        transfers[admission_id] = outcome == 'completed'
    return transfers


async def unfold_for_checkpoint(authority, live, ref, row):
    """A turn parking for a Hermes update gives its folded rows back to their FIFO places first (a steering
    transfer refuses the checkpoint); the continuation folds them again when it starts."""
    entry = folded(authority).pop(ref.session_id, None)
    if entry is None or entry[0] != row['generation']:
        return
    from gateway.session_runtime_workers import tracked_write
    from hermes_state_runtime import revert_steer_input
    for admission_id in sorted(entry[1]):
        await tracked_write(authority, partial(revert_steer_input, authority.db, epoch=authority.epoch,
                                               admission_id=admission_id, generation=row['generation']))
        with live.event_stream.lock:
            transfers = authority.steer_transfers.get(ref.session_id)
            if transfers is not None and transfers[0] == row['generation']:
                transfers[1].pop(admission_id, None)
                if not transfers[1]:
                    authority.steer_transfers.pop(ref.session_id, None)
    logger.info('Unfolded %d admission(s) for an update pause: session=%s admission=%s',
                len(entry[1]), ref.session_id, row['admission_id'])
