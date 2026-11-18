"""Claim publication: storage waits off-loop, execution and stream state stay on-loop."""
import asyncio
from dataclasses import asdict

from gateway.session_contract import SessionHandle
from gateway.session_queued_controls import _INTERACTIVE_SOURCES, queued_controls
from hermes_state_runtime import STEERING, RuntimeStoreError, get_session_admission


def _view_token(authority, ref, live):
    """Caller holds the stream lock; copy overlays, never lend mutable owner state to a worker."""
    entry = authority.steer_transfers.get(ref.session_id)
    transfers = (entry[0], dict(entry[1])) if entry is not None else None
    return (live.event_stream.sequence, dict(live.settling), transfers,
            authority.launch_sources.get(ref.session_id), live.event_stream.epoch)


def _prepare(authority, ref, token):
    from gateway.session_authority import list_session_admissions
    session = authority.db.get_session(ref.session_id)
    if session is None:
        raise RuntimeStoreError('not_found')
    pending = {row['admission_id']: row for row in
               list_session_admissions(authority.db, session_id=ref.session_id)}
    pending.update(token[1])  # a committed but unpublished settlement still reads started
    state = 'unknown' if any(r['status'] == 'unknown' for r in pending.values()) else (
        'running' if any(r['status'] == 'started' for r in pending.values()) else 'idle')
    handle = SessionHandle(ref, authority.instance_id, authority.epoch, session['runtime_revision'],
                           session['runtime_generation'], state)
    transfers = token[2]
    for admission_id in transfers[1] if transfers is not None else ():
        row = get_session_admission(authority.db, admission_id=admission_id)
        if row is not None and row['status'] == 'terminal' and row['outcome'] == STEERING:
            pending[admission_id] = row
    source = token[3] if token[3] is not None else session['source']
    interactive = source in _INTERACTIVE_SOURCES
    # Match _interactive's lazy cache fill: only a row eligible for controls
    # consults the source. Return the intent; only the owner may mutate its cache.
    freeze_source = source if any(queued_controls(authority, row, interactive=True)
                                  for row in pending.values()) else None
    return (handle, [asdict(authority._pending_receipt(row, interactive=interactive))
                     for row in sorted(pending.values(), key=lambda row: row['seq'])]), freeze_source


async def stamp_claim(authority, ref, live, row):
    # Stamp immediately after the commit, even if the subsequent storage read fails.
    # The encompassing tracked_write shields BOTH phases from observer cancellation.
    authority._stamp_claim(ref, live, row, publish_pending=False)
    if row:
        while True:
            with live.event_stream.lock:
                token = _view_token(authority, ref, live)
            prepared, freeze_source = await asyncio.to_thread(_prepare, authority, ref, token)
            with live.event_stream.lock:
                # A submission/cancel/settlement published while we read: never overwrite
                # its newer view. Transfers and settling can change before publication too.
                if token != _view_token(authority, ref, live):
                    continue
                if freeze_source is not None:
                    authority.launch_sources.setdefault(ref.session_id, freeze_source)
                authority._publish_pending(ref, prepared=prepared)
                break
    return row
