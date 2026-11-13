"""Durable update checkpoints are unfinished FIFO heads, never terminal results.

The drain owns the entire handler unwind before entering this module. Parking and
Stop serialize with claims, but no stream lock spans a write or an await.
"""
import asyncio
import logging
from functools import partial

from gateway.session_update_pause_hook import fence_root, is_paused
from hermes_state_update_pause import checkpoint_session_input, get_session_checkpoint

UPDATE_PAUSED = 'update_paused'


def paused_result(authority, row):
    return is_paused((authority.pending_results.get(row['admission_id']) or {}).get('result'))


def update_held(authority):
    from gateway.session_tui_ops import _profile_home
    from hermes_cli.admission_fence import is_held
    return is_held(fence_root(_profile_home(authority)) / 'core-update')


def checkpoint_head(authority, ref):
    # A cached receipt is only a scheduling hint. Consumption always recomputes
    # the proof transactionally; parked polls must not hash a multi-MB tool tail.
    cached = authority.update_parked.get(ref.session_id)
    if cached is not None:
        # Cheap row/key probe (no transcript hash): an external settle must not leave a
        # terminal head parked behind a hold, starving the queued follower.
        from hermes_state_update_pause import CHECKPOINT_PREFIX
        from hermes_state_runtime import RuntimeStoreError
        with authority.db._read_ctx() as conn:
            live = conn.execute('SELECT a.status, (SELECT 1 FROM state_meta WHERE key=?) FROM '
                'session_admissions a WHERE a.admission_id=?',
                (CHECKPOINT_PREFIX + cached['admission_id'], cached['admission_id'])).fetchone()
        if live is not None and live[0] == 'started':
            if live[1]:
                return cached
            # Still the started head but its proof vanished: unknown state, fail closed.
            raise RuntimeStoreError('invalid_checkpoint')
        authority.update_parked.pop(ref.session_id, None)
    receipt = get_session_checkpoint(authority.db, session_id=ref.session_id)
    if receipt is not None:
        authority.update_parked[ref.session_id] = receipt
    else:
        from hermes_state_update_pause import CHECKPOINT_PREFIX
        from hermes_state_runtime import RuntimeStoreError
        with authority.db._read_ctx() as conn:
            invalid = conn.execute("SELECT 1 FROM session_admissions a JOIN state_meta m "
                "ON m.key=? || a.admission_id WHERE a.target_session_id=? AND a.status='started'",
                (CHECKPOINT_PREFIX, ref.session_id)).fetchone()
        if invalid:
            raise RuntimeStoreError('invalid_checkpoint')
    return receipt


async def refuse_checkpoint(authority, ref, live, row):
    """Fence only the rejected started identity. Invalid proof cannot authorize settlement
    or replay; the existing Discard control resolves the unknown head explicitly."""
    from gateway.session_runtime_workers import tracked_write, persist_work_count
    from hermes_state_runtime import _epoch, _admission
    from hermes_state_update_pause import CHECKPOINT_PREFIX, delete_session_checkpoint

    def write(conn):
        _epoch(conn, authority.epoch)
        current = _admission(conn, row['admission_id'])
        if (current['status'], current['owner_epoch'], current['generation']) != (
                'started', row['owner_epoch'], row['generation']):
            return False
        if not conn.execute('SELECT 1 FROM state_meta WHERE key=?',
                            (CHECKPOINT_PREFIX + row['admission_id'],)).fetchone():
            return None  # ordinary running work, not this checkpoint protocol
        conn.execute("UPDATE session_admissions SET status='unknown' WHERE admission_id=?",
                     (row['admission_id'],))
        delete_session_checkpoint(conn, row['admission_id'])
        conn.execute('UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id=?',
                     (ref.session_id,))
        return True

    async with live.claim_gate, live.mutation_lock:
        changed = await tracked_write(authority, partial(authority.db._execute_write, write))
        if changed:
            authority.update_parked.pop(ref.session_id, None)
            authority._publish_pending(ref)
            persist_work_count(authority)
        return changed


async def resume_checkpoint(authority, ref, live, first):
    """Recheck the hold at the consume transaction, after asynchronous route authorization."""
    from gateway.session_runtime_workers import tracked_write
    from hermes_state_runtime import RuntimeStoreError
    from hermes_state_update_pause import resume_session_checkpoint
    gate = authority._admission_gate()
    def guard(conn):
        gate(conn)
        if update_held(authority):
            raise RuntimeStoreError('update_pause_held')
    def resumed(row):
        authority.update_parked.pop(ref.session_id, None)
        authority._stamp_claim(ref, live, row)
        from gateway.session_runtime_workers import persist_work_count
        persist_work_count(authority)
        # [daveey] The gateway twin of the Desktop sweep's line: hub checks grep it per session.
        logging.getLogger(__name__).info('Update pause: auto-continue scheduled for gateway session=%s '
                                         'admission=%s', ref.session_id, row.get('admission_id'))
        return row

    try:
        row = await tracked_write(authority, partial(resume_session_checkpoint, authority.db,
            epoch=authority.epoch, admission_id=first['admission_id'], _guard=guard),
            then=resumed)
    except RuntimeStoreError as exc:
        if exc.reason != 'update_pause_held':
            raise
        return UPDATE_PAUSED
    return row


async def park_or_interrupt(authority, live, ref, row):
    """Tracked by the caller before it yields; retain the waiter and media on success."""
    from gateway.session_runtime_workers import tracked_write, persist_work_count
    async with live.claim_gate, live.mutation_lock:
        remember_claim(authority, ref.session_id, row)
        if stopped(authority, row):
            return await settle_interrupted(authority, live, ref, row)
        if unstarted_result(authority, row):
            requeued = await requeue_unstarted(authority, live, ref, row)
            if requeued is not None:
                return requeued
        from gateway.session_fold_pending import checkpoint_folded, release_checkpointed
        try:
            receipt = await tracked_write(authority, partial(checkpoint_session_input, authority.db,
                epoch=authority.epoch, admission_id=row['admission_id'], generation=row['generation'],
                physical_session_id=authority.physical_target(ref), transcript_mark=row['transcript_mark'],
                folded=checkpoint_folded(authority, row)))
        except Exception:
            logging.getLogger(__name__).exception('Update checkpoint failed for %s', row['admission_id'])
            return await settle_interrupted(authority, live, ref, row)
        release_checkpointed(authority, live, ref, row)
        if stopped(authority, row):
            return await settle_interrupted(authority, live, ref, row)
        authority.pending_results.pop(row['admission_id'], None)
        authority.update_parked[ref.session_id] = receipt
        with live.event_stream.lock:
            live.event_stream.execution = {}
            authority.adopted.pop(ref.session_id, None)
        logging.getLogger(__name__).info('Parked gateway turn for update: session=%s admission=%s generation=%s',
                                         ref.session_id, row['admission_id'], row['generation'])
        authority._publish_pending(ref)
        persist_work_count(authority)
        return True


def unstarted_result(authority, row):
    """[daveey] t_01a1d49d50e4c656: the paused result of a turn that yielded before it started (no boundary)."""
    from agent.update_pause_cancel import UNSTARTED_KEY
    result = (authority.pending_results.get(row['admission_id']) or {}).get('result')
    return isinstance(result, dict) and result.get(UNSTARTED_KEY) is True


async def requeue_unstarted(authority, live, ref, row):
    """Hand an unstarted claim back to its FIFO place (caller holds the claim gate and mutation lock); the
    waiter and media stay with the queued row. None when the store refuses: the caller's checkpoint path
    then decides (and settles it interrupted, as any unparkable pause).

    [daveey] t_01a1d4a0ecd9bd54: a Stop that ``interrupt_update`` latched while the write ran (it found the claim
    still started and told its caller the turn stops) cancels the re-queued row instead of letting the notice run
    after the hold (``_stopped_after_requeue``).

    [daveey] t_01a1d4cc43b7ac4a: one Stop window is left, between the write's commit and this coroutine resuming.
    It is benign: ``interrupt_update`` then reads the row queued, not started, and returns False, so its caller is
    told the turn does not stop and the notice runs once after the hold, as for a Stop after this function returns.
    No await separates the ``stopped`` read below from clearing the claim, so a latch is never dropped unread."""
    from gateway.session_runtime_workers import tracked_write, persist_work_count
    from gateway.session_settlement_recovery import _transfers
    from hermes_state_runtime import RuntimeStoreError
    from hermes_state_update_pause import requeue_unstarted_input
    # [daveey] t_01a1d4d4c30c7e43: read after execution returned (no steer can reach the turn now); each
    # undelivered transfer goes back to its own FIFO place in the same write, behind the wake.
    transfers = _transfers(authority, live, row) or {}
    try:
        await tracked_write(authority, partial(requeue_unstarted_input, authority.db, epoch=authority.epoch,
            admission_id=row['admission_id'], generation=row['generation'], transcript_mark=row['transcript_mark'],
            transfers=transfers))
    except RuntimeStoreError as exc:
        detail = getattr(exc, 'detail', None)
        logging.getLogger(__name__).warning('Unstarted gateway turn could not be re-queued for update: session=%s '
                                            'admission=%s (%s%s)', ref.session_id, row['admission_id'], exc,
                                            f': {detail}' if detail else '')
        return None
    stopped_meanwhile = stopped(authority, row)  # read before the claim's latch is cleared below
    authority.pending_results.pop(row['admission_id'], None)
    previous = authority.update_claims.pop(ref.session_id, None)
    if previous is not None:
        authority.update_stops.discard(previous[0])
    with live.event_stream.lock:
        live.event_stream.execution = {}
        authority.adopted.pop(ref.session_id, None)
        entry = authority.steer_transfers.get(ref.session_id)
        if entry is not None and entry[0] == row['generation']:
            authority.steer_transfers.pop(ref.session_id, None)
    if stopped_meanwhile:
        return await _stopped_after_requeue(authority, ref, row)
    logging.getLogger(__name__).info('Re-queued unstarted gateway turn for update hold: session=%s admission=%s '
                                     'generation=%s%s', ref.session_id, row['admission_id'], row['generation'],
                                     f' (with {len(transfers)} steer transfer(s))' if transfers else '')
    authority._publish_pending(ref)
    persist_work_count(authority)
    return True


async def _stopped_after_requeue(authority, ref, row):
    """[daveey] t_01a1d4a0ecd9bd54: a Stop latched while the requeue write ran. ``interrupt_update`` found the claim
    still started and told its caller the turn stops; its own settlement (``finish``) waits on the claim gate the
    caller holds and would then find the row queued. Honour it here: cancel the re-queued row (it never ran, so
    ``cancelled`` is its exact outcome), answer its waiter and clear the latch, so the notice never runs after the
    hold and a later claim of the session inherits no Stop. A Stop that lands after this function returns finds the
    row queued (not started) and is refused, as for any queued row."""
    from gateway.session_ingress_media import release_admission_media
    from gateway.session_runtime_workers import tracked_write, persist_work_count
    from hermes_state_runtime import cancel_session_input, get_session_admission
    admission_id, sid = row['admission_id'], ref.session_id
    authority.update_stops.discard(admission_id)
    if authority.pending_stops.get(sid) == row['generation']:
        authority.pending_stops.pop(sid, None)
        authority.pending_stop_causes.pop(sid, None)
    try:
        cancelled = await tracked_write(authority, partial(cancel_session_input, authority.db,
                                                           epoch=authority.epoch, admission_id=admission_id))
    except Exception:
        # [daveey] t_01a1d4cc43b7ac4a: no retry here, since the store already retries a locked or busy writer
        # within its write patience. What reaches this point is not transient, or its commit is unknown (an
        # IOERR at COMMIT), so read the row back. A cancel that committed settles below. Otherwise the row stays
        # queued and runs once after the hold. Fail loud, never silent.
        logging.getLogger(__name__).exception('Update requeue: cancelling stopped %s failed', admission_id)
        try:
            cancelled = await asyncio.to_thread(get_session_admission, authority.db, admission_id=admission_id)
        except Exception:
            logging.getLogger(__name__).exception('Update requeue: reading stopped %s back failed', admission_id)
            cancelled = None
        if cancelled is None or (cancelled['status'], cancelled['outcome']) != ('terminal', 'cancelled'):
            logging.getLogger(__name__).error('Update requeue: stopped %s stays queued and runs after the hold',
                                              admission_id)
    if cancelled is not None and (cancelled['status'], cancelled['outcome']) == ('terminal', 'cancelled'):
        authority._settle_cancelled(ref, admission_id)
        await asyncio.to_thread(release_admission_media, authority.db, admission_id)
        logging.getLogger(__name__).info('Re-queued gateway turn was stopped during the requeue; cancelled: '
                                         'session=%s admission=%s generation=%s', sid, admission_id,
                                         row['generation'])
    authority._publish_pending(ref)
    persist_work_count(authority)
    return True


def stopped(authority, row):
    return (row['admission_id'] in authority.update_stops
            or authority.pending_stops.get(row['target_session_id']) == row['generation']
            or authority.delivered_stops.get(row['target_session_id']) == row['generation'])


def claim_started(authority, sid, row):
    if row.get('update_resume'):
        remember_claim(authority, sid, row)
    else:
        previous = authority.update_claims.pop(sid, None)
        if previous is not None:
            authority.update_stops.discard(previous[0])


def remember_claim(authority, sid, row):
    admission_id, generations = authority.update_claims.get(sid, (row['admission_id'], set()))
    if admission_id != row['admission_id']:
        authority.update_stops.discard(admission_id)
        generations = set()
    generations.add(row['generation'])
    if row.get('update_resume'):
        generations.add(row['update_resume']['generation'])
    authority.update_claims[sid] = (row['admission_id'], generations)


async def interrupt_update(authority, ref, generation):
    """Latch before waiting on a commit. Old-generation Stop maps ONLY to its resumed admission."""
    from hermes_state_runtime import get_session_admission
    from gateway.session_runtime_workers import track_mutation
    sid = ref.session_id
    live = authority.sessions[sid]
    admission_id, generations = authority.update_claims.get(sid, (None, set()))
    receipt = (authority.update_parked.get(sid)
               or get_session_checkpoint(authority.db, session_id=sid))
    execution = live.event_stream.execution
    if admission_id is None and receipt is not None:
        admission_id, generations = receipt['admission_id'], {receipt['generation']}
    if admission_id is None and execution.get('admission_id'):
        candidate = get_session_admission(authority.db, admission_id=execution['admission_id'])
        if candidate is not None and paused_result(authority, candidate):
            admission_id, generations = candidate['admission_id'], {candidate['generation']}
    if admission_id is None or generation not in generations:
        return False
    row = get_session_admission(authority.db, admission_id=admission_id)
    if row is None or row['status'] != 'started':
        return False
    # A fresh successor must never inherit a checkpoint generation's Stop.
    if execution and execution.get('admission_id') != admission_id:
        return False
    authority.update_stops.add(admission_id)
    if authority.executing.get(sid) == row['generation']:
        authority.pending_stops[sid] = row['generation']
        with live.event_stream.lock:
            adopted_generation, agent = authority.adopted.get(sid, (None, None))
        if adopted_generation == row['generation'] and agent is not None:
            from agent.interrupt_compat import request_hard_interrupt
            request_hard_interrupt(agent)
        return True

    async def finish():
        async with live.claim_gate, live.mutation_lock:
            current = get_session_admission(authority.db, admission_id=admission_id)
            if current is not None and current['status'] == 'started':
                if authority.executing.get(sid) == current['generation']:
                    authority.pending_stops[sid] = current['generation']
                else:
                    await settle_interrupted(authority, live, ref, current)
    await asyncio.shield(track_mutation(authority, finish()))
    return True


async def settle_interrupted(authority, live, ref, row):
    from gateway.session_settlement_recovery import settle_and_deliver
    if row['owner_epoch'] != authority.epoch:
        # Stop can arrive before recovery schedules the parked head. Adopt its exact
        # proof once, solely to settle it: never run the original input again.
        from gateway.session_runtime_workers import tracked_write
        from hermes_state_update_pause import resume_session_checkpoint
        row = await tracked_write(authority, partial(resume_session_checkpoint, authority.db,
            epoch=authority.epoch, admission_id=row['admission_id']))
    authority.update_parked.pop(ref.session_id, None)
    authority.pending_deliveries.pop(row['admission_id'], None)
    authority.pending_results[row['admission_id']] = {
        'result': {'interrupted': True, 'final_response': ''}, 'usage': {}}
    return await settle_and_deliver(authority, live, ref, row, '', 'interrupted')
