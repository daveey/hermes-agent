"""An async completion whose admission failed is delivered again, once (deleg_e757744d).

A completion's ledger row is acknowledged ``delivered`` when its admission commits, before the
admission runs. An admission that then settles ``failed`` (its note met another turn's lease, before
``session_completion_notes._write_note`` waited for it) left the row ``delivered`` with nothing in the
parent. Its request id is admitted once, so a later offer of the same result resolves to that failed
admission and cannot run it again.

Two halves close it:

- ``reconcile_failed_completion_admissions`` (once per gateway start, bounded) finds local completion
  admissions that settled ``failed`` with no note and no copy of their text in the parent, and reopens
  each async delegation they carried through ``requeue_admitted_completion``. A delegation whose
  Desktop row (``async_delegation_complete`` stamped with its id) is in the parent counts as
  delivered, and the reopen records the admission it was made for (``recovered_admission_id``), so a
  (delegation, admission) pair is reopened at most once across starts. The ledger stays the
  gate: the reopened row is claimed (``claim_completion_delivery``, one attempt each) like any pending
  result, and a row past its attempts or its replay age is dropped there with a logged reason.
- ``deliver_admitted`` is the claimed offer's step when the result already has an admission: a
  failed local admission without a note gets its note now (keyed by that admission, so exactly once);
  any other admission is the lost acknowledgement it always was.
"""
from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

# Newest failed admissions one start examines: a bounded pass over history, not a crawl of it.
RECONCILE_SCAN_LIMIT = 200

_FAILED_SQL = ("SELECT * FROM session_admissions WHERE status = 'terminal' AND outcome = 'failed' "
               "ORDER BY seq DESC LIMIT ?")
_NOTED_SQL = ("SELECT 1 FROM messages WHERE session_id IN (?, ?) AND "
              "(json_extract(display_metadata, '$.completion_admission') = ? OR content = ?) LIMIT 1")
# A Desktop consumer ("tui-poller") writes its own row for the delegation: no admission id, and its
# content lacks the gateway's INTERNAL NOTIFICATION footer, so only the delegation id names it.
_DELIVERED_SQL = ("SELECT 1 FROM messages WHERE session_id IN (?, ?) AND "
                  "display_kind = 'async_delegation_complete' AND "
                  "json_extract(display_metadata, '$.delegation_id') = ? LIMIT 1")


def _local_descriptor(row):
    return (row.get('payload') or {}).get('local_automation_v1')


def _delegation_ids(descriptor) -> list[str]:
    """The async delegations a completion admission carries (a coalesced batch carries several)."""
    ids = []
    for identity in [descriptor.get('identity'), *(descriptor.get('identities') or ())]:
        try:
            kind, producer, _rest = json.loads(identity)
        except (TypeError, ValueError):
            continue
        if kind == 'async_delegation' and producer and producer not in ids:
            ids.append(producer)
    return ids


def _target(authority, row) -> str:
    from gateway.session_viewer_close import _physical_target
    return _physical_target(authority, row['target_session_id'])


def _noted(authority, row, target) -> bool:
    """The admission's result already reached its session: its note, or its text from a turn that ran."""
    with authority.db._read_ctx() as conn:
        return conn.execute(_NOTED_SQL, (row['target_session_id'], target, row['admission_id'],
                                         str(row['payload'].get('text') or ''))).fetchone() is not None


def _delivered(authority, row, target, delegation_id) -> bool:
    """The delegation's result reached the session through a consumer that stamps its id."""
    with authority.db._read_ctx() as conn:
        return conn.execute(_DELIVERED_SQL, (row['target_session_id'], target,
                                             delegation_id)).fetchone() is not None


def unnoted_failed_completions(authority, limit: int = RECONCILE_SCAN_LIMIT) -> list[tuple[str, dict]]:
    """``(delegation_id, admission)`` for each async delegation carried by a local completion admission
    that settled ``failed`` and left nothing in its session (per delegation: a coalesced admission
    yields only the ids no consumer delivered)."""
    from hermes_state_runtime import _row
    with authority.db._read_ctx() as conn:
        rows = [_row(r) for r in conn.execute(_FAILED_SQL, (limit,)).fetchall()]
    found = []
    for row in rows:
        descriptor = _local_descriptor(row)
        ids = _delegation_ids(descriptor) if descriptor else []
        if not ids:
            continue
        target = _target(authority, row)
        if _noted(authority, row, target):
            continue
        found.extend((delegation_id, row) for delegation_id in ids
                     if not _delivered(authority, row, target, delegation_id))
    return found


def reconcile_failed_completion_admissions(authority, target_queue) -> int:
    """Reopen each acknowledged delegation whose admission failed unnoted; the count reopened."""
    from tools.async_delegation import requeue_admitted_completion
    reopened = 0
    for delegation_id, row in unnoted_failed_completions(authority):
        # One reopen per (delegation, failed admission), whichever consumer then delivers it (a
        # batch row stamps no delegation id, so the transcript alone cannot say it arrived).
        if requeue_admitted_completion(delegation_id, target_queue,
                                       recovered_admission_id=row['admission_id']):
            reopened += 1
            logger.warning('Async delegation %s: its admission %s on %s failed before delivery; offered again',
                           delegation_id, row['admission_id'], row['target_session_id'])
    return reopened


async def reconcile_at_start(runner) -> None:
    """The gateway start's one pass over the launch profile's ledger (the watcher's first step).
    Best-effort: a failed pass leaves every row as it was for the next start."""
    from gateway.session_authorities import active_authority
    from tools.process_registry import process_registry
    authority = active_authority(runner)
    if authority is None:
        return
    try:
        count = await asyncio.to_thread(reconcile_failed_completion_admissions, authority,
                                        process_registry.completion_queue)
    except Exception:  # health: allow BLE001 -- a recovery pass must never stop the completion watcher
        logger.warning('Failed-admission completion reconciliation did not run', exc_info=True)
        return
    if count:
        logger.info('Offered %d async completion(s) whose admission failed again', count)


async def deliver_admitted(runner, admission) -> bool:
    """The claimed offer of a result that already has *admission*: True when it is delivered (a lost
    acknowledgement, or the failed admission's note written now), False to release the claim."""
    if (admission.get('status'), admission.get('outcome')) != ('terminal', 'failed'):
        return True
    descriptor = _local_descriptor(admission)
    if descriptor is None:
        return True  # a platform turn's failure is its own; only a local note can be written here
    from gateway.session_authorities import active_authority
    from gateway.session_completion_notes import note_metadata
    authority = active_authority(runner)
    if authority is None:
        return False
    target = await asyncio.to_thread(_target, authority, admission)
    if await asyncio.to_thread(_noted, authority, admission, target):
        return True
    metadata = {**note_metadata(descriptor), 'recovered_admission': True}
    try:
        await asyncio.to_thread(
            authority.db.append_completion_note, target, admission['admission_id'],
            admission['payload']['text'], descriptor.get('display_kind', 'internal_notification'), metadata)
    except Exception as exc:  # health: allow BLE001 -- any refusal (a held lease) spends this attempt; the ledger retries or drops
        logger.warning('Note for failed admission %s on %s not written yet: %s',
                       admission['admission_id'], target, exc)
        return False
    logger.info('Async completion of failed admission %s recovered as a note on %s',
                admission['admission_id'], target)
    return True
