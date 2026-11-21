"""Execution-scoped worker operations on the ordinary authenticated authority.

Only explicit local compute registrations are enabled here. A delegated child is reserved by
the owner on its parent's receipt stream (gateway/session_worker_child.py), never registered.
"""
import asyncio
import hmac
import logging
import os
import time

from gateway.session_admission import admission_fingerprint
from hermes_state_runtime import (
    WORKER_RECEIPT_TIMING, RuntimeStoreError, _secret_digest, adopt_worker_execution,
    mutate_worker_execution, register_worker_execution,
)

logger = logging.getLogger(__name__)

_SCOPE = {'profile_id', 'session_id', 'execution_id', 'generation', 'pid', 'birth', 'secret'}

#: A ``worker.persist`` slower than this is logged: the worker waits ``worker_receipt_timeout_s()``
#: for the reply, and before this line a stall stayed invisible until a worker died of it.
SLOW_WORKER_PERSIST_S = 5.0


async def timed_persist(params, persist):
    """Await ``persist()`` (one ``worker.persist`` request) and log it once if it was slow.

    The WARNING names the operation, the session, how long its write waited for the state.db lock
    (``n/a`` when it never reached the write), the total handler time and the outcome.
    """
    timing = {}
    token = WORKER_RECEIPT_TIMING.set(timing)
    started, outcome = time.monotonic(), 'ok'
    try:
        return await persist()
    except BaseException as exc:
        outcome = getattr(exc, 'reason', None) or type(exc).__name__
        raise
    finally:
        WORKER_RECEIPT_TIMING.reset(token)
        total = time.monotonic() - started
        if total >= SLOW_WORKER_PERSIST_S:
            lock_wait = timing.get('lock_wait_s')
            logger.warning('slow worker.persist: operation=%s session=%s lock_wait=%s total=%.1fs outcome=%s',
                           params.get('operation'), params.get('session_id'),
                           'n/a' if lock_wait is None else f'{lock_wait:.1f}s', total, outcome)


def _claim(connection, ref, params):
    import psutil
    authority = connection.authority
    if params.get('profile_id') != authority.profile_id or connection.actor.profile_id != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    if not connection.actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    pid, birth = params.get('pid'), params.get('birth')
    secret = params.get('secret')
    if (type(pid) is not int or pid <= 0 or pid == os.getpid()
            or type(birth) not in (int, float) or not isinstance(secret, str) or not 16 <= len(secret) <= 256):
        raise RuntimeStoreError('invalid_params')
    try:
        process = psutil.Process(pid)
        from hermes_cli.process_identity import worker_birth
        if (worker_birth(process) != birth or not process.is_running()
                or process.status() == psutil.STATUS_ZOMBIE
                or process.username() != psutil.Process().username()):
            raise RuntimeStoreError('permission_denied')
    except psutil.Error as exc:
        raise RuntimeStoreError('worker_not_live') from exc
    # Reuse the durable adoption digest as the producer claim. Changing any
    # assignment, principal, profile, PID birth or secret cannot adopt it.
    from hermes_state_worker_compression import worker_claim_origin
    origin = worker_claim_origin(authority.db, params)
    return admission_fingerprint(canonical_target=origin, payload={
        key: params[key] for key in _SCOPE} | {'session_id': origin, 'principal': connection.actor.subject})


def _verify(connection, ref, params, claim):
    from hermes_state_worker_compression import worker_retry_target
    authority = connection.authority
    target = worker_retry_target(authority.db, params)
    with authority.db._read_ctx() as conn:
        row = conn.execute('SELECT * FROM worker_executions WHERE execution_id=?',
                           (params['execution_id'],)).fetchone()
        if row is None:
            raise RuntimeStoreError('not_found')
        if row['session_id'] != target:
            raise RuntimeStoreError('permission_denied')
        if type(params['generation']) is not int or row['generation'] != params['generation']:
            raise RuntimeStoreError('stale_generation')
        if not hmac.compare_digest(row['adoption_digest'], _secret_digest(claim)):
            raise RuntimeStoreError('permission_denied')


async def worker_request(connection, ref, params, *, operation):
    if operation == 'persist':
        result = await timed_persist(params, lambda: _worker_request(connection, ref, params, operation=operation))
        return stamp_worker_receipt(result, params)
    return await _worker_request(connection, ref, params, operation=operation)


def stamp_worker_receipt(result, params):
    """Name the committed write a ``worker.persist`` reply answers. Only reached once the receipt
    transaction returned (a refusal or failed commit raises instead), and never stored: the worker
    drops a journal entry only on a reply naming exactly that (execution, sequence). Correlating by
    the RPC id alone once let a reply that was not this write's receipt advance the journal past a
    write the owner never committed (t_01a1a8fa8be4217f: owner at 272, journal acked 273)."""
    if not isinstance(result, dict):
        return result
    return {**result, '_receipt': {'execution_id': params.get('execution_id'), 'sequence': params.get('sequence')}}


async def _worker_request(connection, ref, params, *, operation):
    from gateway.session_authorities import require_serving
    require_serving(connection.authority)  # adopt/persist would write with the released epoch
    if operation == "persist" and isinstance(params.get("operation"), str) and params["operation"].startswith("delegation."):
        from gateway.session_worker_delegation import worker_delegation_request
        return await worker_delegation_request(connection, ref, params)
    if operation == "persist" and params.get("operation") == "child.reserve":
        from gateway.session_worker_child import worker_child_request
        return await worker_child_request(connection, ref, params)
    extra = {'register': {'kind'}, 'adopt': set(), 'persist': {'epoch', 'sequence', 'operation', 'payload'},
             'turnLease': {'epoch', 'holder'}, 'nativeOrigin': {'epoch', 'holder'},
             'sessionContext': {'epoch', 'target'}, 'credentials': {'epoch'}}[operation]
    if set(params) != _SCOPE | extra:
        raise RuntimeStoreError('invalid_params')
    claim = _claim(connection, ref, params)
    authority = connection.authority
    if operation == 'register':
        authority._require_admission_open()
    scope = {key: params[key] for key in ('execution_id', 'session_id', 'generation')}
    if operation == 'register':
        # Only a worker-adoption ticket (what a launched worker carries): an interactive client
        # must not park a 'registered' row that nothing reaps (the FIFO would pause until restart).
        authority.authorize(connection.actor, ref, 'worker:adopt')
        if params['kind'] != 'compute':
            raise RuntimeStoreError('unsupported_producer')
        return await asyncio.to_thread(register_worker_execution, authority.db, epoch=authority.epoch,
            **scope, kind='compute', adoption_secret=claim, require_idle=True)
    if 'worker:adopt' not in connection.actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    _verify(connection, ref, params, claim)
    if operation == 'sessionContext':
        from hermes_state_worker_observations import worker_session_context
        if type(params['epoch']) is not int or params['epoch'] != authority.epoch:
            raise RuntimeStoreError('stale_epoch')
        return await asyncio.to_thread(worker_session_context, authority.db, epoch=authority.epoch,
                                       **scope, target=params['target'])
    if operation == 'credentials':
        if type(params['epoch']) is not int or params['epoch'] != authority.epoch:
            raise RuntimeStoreError('stale_epoch')
        with authority.db._read_ctx() as conn:
            row = conn.execute('SELECT status, owner_epoch FROM worker_executions WHERE execution_id=?',
                               (params['execution_id'],)).fetchone()
        if row is None or row['status'] != 'running' or row['owner_epoch'] != authority.epoch:
            raise RuntimeStoreError('stale_generation')
        from gateway.session_worker_credentials import resolve_credentials
        return resolve_credentials(authority, params)
    if operation in ('turnLease', 'nativeOrigin'):
        from hermes_state_runtime import worker_turn_lease
        if type(params['epoch']) is not int or params['epoch'] != authority.epoch:
            raise RuntimeStoreError('stale_epoch')
        return await asyncio.to_thread(worker_turn_lease, authority.db, epoch=authority.epoch,
                                       **scope, holder=params['holder'], native_origin=operation == 'nativeOrigin')
    if operation == 'adopt':
        return await asyncio.to_thread(adopt_worker_execution, authority.db, epoch=authority.epoch,
                                       **scope, adoption_secret=claim)
    result = await asyncio.to_thread(mutate_worker_execution, authority.db, epoch=params['epoch'],
        **scope, sequence=params['sequence'], operation=params['operation'], payload=params['payload'])
    if params['operation'] == 'execution.finish':
        # Queued input parked behind this worker; nothing else would restart its FIFO.
        authority.wake_after_worker(params['session_id'])
    return result
