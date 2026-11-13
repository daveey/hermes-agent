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
claim completed (``steered``, acked once); any other ending (failed, interrupted, unknown, an unstarted
requeue) puts it back in its own FIFO place (``_requeue_transfer``), so nothing is lost and nothing settles
twice. A pause for an update settles it ``steered`` once the turn's user message (which carries its text)
is persisted, because the continuation resumes that history (``checkpoint_folded``). The queue's own fold
rules and the rows themselves are unchanged.
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


def _category(row):
    return _automation(row['payload']).get('notification_category', 'result')


def _foldable(row, category):
    payload = row['payload']
    descriptor = _automation(payload)
    if not descriptor or _producer(row) not in FOLDABLE_PRODUCERS or not isinstance(payload.get('text'), str):
        return False
    # The turn's mute is the head's (a diagnostic wake hides its turn when warnings are off): a result
    # folded into a muted turn would settle 'steered' unseen, so only the head's own category joins it
    # (the admission fold's rule, ``session_automation._waiting_completion``).
    if descriptor.get('notification_category', 'result') != category:
        return False
    # A remote Kanban notice is acked by the keys its own turn's user row carries: it keeps its own turn.
    return not descriptor.get('kanban_event_keys')


def candidates(authority, ref, head):
    """Queued rows behind ``head`` that may join it, in admission order. A held notifier wake and other
    producer rows (liveness ticks, loop ticks) are passed over; the first human input ends the scan, so a
    notice never jumps a prompt queued before it."""
    from gateway.session_notifier_drain import eligible
    from gateway.session_completion_notes import completion_is_note
    picked, category = [], _category(head)
    for row in list_session_admissions(authority.db, session_id=ref.session_id):
        if row['status'] != 'queued' or row['admission_id'] == head['admission_id']:
            continue
        if not _automation(row['payload']):
            break
        if eligible(row) and _foldable(row, category) and not completion_is_note(authority, ref, row):
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
    # The fold is optional: an unexpected error stops it and the head runs as it would have (never
    # 'failed' for a row it only tried to carry). The rows not taken keep their own FIFO turns.
    try:
        pending = await asyncio.to_thread(candidates, authority, ref, row)
    except Exception:
        logger.warning('Fold scan failed; the turn runs alone: session=%s', sid, exc_info=True)
        pending = []
    for candidate in pending:
        try:
            await _check(authority, ref, live, candidate)
            payload = dict(candidate['payload'])
            await asyncio.to_thread(_refresh, payload)
        except RuntimeStoreError:
            continue  # its own claim refuses it in FIFO order
        except Exception:
            logger.warning('Fold check failed; folding stops: session=%s admission=%s', sid,
                           candidate['admission_id'], exc_info=True)
            break
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
        except Exception:
            # Maybe committed: the fence stays undelivered, so the claim's settlement requeues the row
            # (a no-op when nothing committed), as ``steer_queued`` does.
            logger.warning('Fold transfer failed; folding stops: session=%s admission=%s', sid,
                           candidate['admission_id'], exc_info=True)
            break
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


def checkpoint_folded(authority, row):
    """The rows folded into ``row``'s claim, for its update checkpoint write (``checkpoint_session_input``
    ``folded=``), which settles them in the same transaction (a steering transfer otherwise refuses it).

    [daveey] t_01a1d6d8bf621f17: they were reverted to queued before the checkpoint, but the paused turn's
    user message (head text + folded texts) is already in the persisted history the continuation resumes,
    so the continuation folded them again and the model read each notice twice (once more per further
    pause). Now each settles ``steered`` once that user message is persisted, and goes back to its FIFO
    place only when nothing of the turn was. The entry stays until the write commits: a refused checkpoint
    settles the claim interrupted, and ``settle_outcome`` then gives every row back, as before."""
    entry = folded(authority).get(row['target_session_id'])
    if entry is None or entry[0] != row['generation']:
        return ()
    return tuple(sorted(entry[1]))


def release_checkpointed(authority, live, ref, row):
    """After the checkpoint committed: drop the claim's fold and transfer fences for its folded rows and
    publish each one's settled outcome (``steered`` to its observers; a re-queued row is listed again)."""
    from hermes_state_runtime import get_session_admission
    entry = folded(authority).pop(ref.session_id, None)
    if entry is None or entry[0] != row['generation']:
        return
    with live.event_stream.lock:
        transfers = authority.steer_transfers.get(ref.session_id)
        if transfers is not None and transfers[0] == row['generation']:
            for admission_id in entry[1]:
                transfers[1].pop(admission_id, None)
            if not transfers[1]:
                authority.steer_transfers.pop(ref.session_id, None)
    steered = []
    for admission_id in sorted(entry[1]):
        settled = get_session_admission(authority.db, admission_id=admission_id)
        if settled is not None and settled['status'] == 'terminal' and settled['outcome'] == 'steered':
            steered.append(admission_id)
            authority._settle_cancelled(ref, admission_id, outcome='steered')
    authority._publish_pending(ref)
    logger.info('Settled folded admission(s) with an update checkpoint: session=%s admission=%s steered=%s '
                'requeued=%d', ref.session_id, row['admission_id'], ','.join(steered) or '-',
                len(entry[1]) - len(steered))
