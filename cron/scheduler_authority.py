"""Transport-only cron execution; output/delivery remain with the firing scheduler."""
import asyncio
import hashlib
import json
import uuid


class CronExecutionUnknown(RuntimeError):
    """The owner may have accepted this fire; never book it as failed or re-fire."""


class CronFireNotAdmitted(RuntimeError):
    """[daveey] No ready owner was reached and nothing was submitted (the gateway is draining for an
    update, starting, absent, or its socket could not be opened): a scheduled fire is held and stays
    due, never booked as a failed
    run. Raised only before admission; a refusal after submit stays a definite failure."""

    def __init__(self, message, state, reason_code=None):
        super().__init__(message)
        self.state, self.reason_code = state, reason_code


# GatewayUnavailableError states that mean "not ready yet", not "this owner refuses the job".
_NOT_READY_STATES = frozenset({'draining', 'starting', 'absent'})
# Consecutive dial-failure holds of one job before the next one is booked FAILED.
CONNECT_HOLD_LIMIT = 3


def _dial_failed(exc):
    """The gateway socket could not be opened: a plain client error raised from a socket
    error/timeout. Owner refusals (``GatewayRPCError``), not-ready verdicts
    (``GatewayUnavailableError``) and local client refusals (bad URL, protocol mismatch) are not."""
    from hermes_cli.gateway_client import GatewayClientError, GatewayRPCError, GatewayUnavailableError
    return (isinstance(exc, GatewayClientError)
            and not isinstance(exc, (GatewayRPCError, GatewayUnavailableError))
            and isinstance(exc.__cause__, (OSError, TimeoutError)))


def _connect_holds_left(job_id):
    from cron.jobs import get_job
    from cron.unadmitted_fire import connect_holds
    return connect_holds(get_job(job_id)) < CONNECT_HOLD_LIMIT


def settle_unbooked_fire(job, exc, fire_owner, execution_id):
    """[daveey] Settle a fire that must not be booked as a failed run; True when it was handled.

    - CronFireNotAdmitted on a scheduled fire: the gateway was not ready (an update's restart gap)
      and nothing was submitted. The slot goes back on the schedule, the streak and last verdict
      stay, and the ledger row records the non-admission. A manual fire the gateway refused while
      draining is deferred to the next gateway's first tick the same way (cron/restart_cut.py);
      one refused for any other not-ready state still fails loudly.
    - CronExecutionUnknown: the owner may have accepted it, so the job pauses for reconciliation.
    Every other error returns False and takes the failed-run path."""
    import logging
    log = logging.getLogger('cron.scheduler')
    if isinstance(exc, CronFireNotAdmitted) and not job.get('_scheduled_instant') and exc.state == 'draining':
        from cron.restart_cut import defer_unadmitted
        defer_unadmitted(job, execution_id, str(exc), fire_owner or None)
        return True
    if isinstance(exc, CronFireNotAdmitted) and job.get('_scheduled_instant'):
        from cron.executions import finish_execution
        from cron.unadmitted_fire import release_unadmitted_fire
        released = release_unadmitted_fire(
            job['id'], job['_scheduled_instant'], str(exc), expected_fire_owner=fire_owner or None,
            reason_code=exc.reason_code)
        finish_execution(execution_id, success=False, error=str(exc), delivery_outcome='suppressed')
        log.warning('Cron %s held (%s): %s', job['id'],
                    'slot stays due' if released else 'claim moved on', exc)
        return True
    if isinstance(exc, CronExecutionUnknown):
        from cron.executions import give_up_execution
        pause_for_unknown(job['id'], execution_id, str(exc))
        give_up_execution(execution_id, reason=str(exc))
        log.error('Cron %s paused with unverified admission: %s', job['id'], exc)
        return True
    return False


def journal_path(job_id, request_id):
    from hermes_constants import get_hermes_home
    key = hashlib.sha256(json.dumps([job_id, request_id]).encode()).hexdigest()
    return get_hermes_home() / 'cron' / 'admissions' / (key + '.json')


class CronJournalCorrupt(ValueError):
    """[daveey] An admission journal is unreadable or does not carry the identity its name
    encodes. Its owner job cannot be known, so callers hold (never retire) and name ``path``."""

    def __init__(self, path, detail):
        super().__init__(f'cron journal {detail}: {path}')
        self.path = path


def _job_journals(job_id, *, skip=None):
    """Yield ``(path, record)`` for each identity-checked admission journal of ``job_id``.

    The one home of the journal identity rule: a journal names its job and request in
    ``params`` and must live at ``journal_path`` of exactly that pair. An unreadable journal
    raises CronJournalCorrupt naming its path, because its owner may be this job.
    """
    root = journal_path(job_id, '').parent
    for path in sorted(root.glob('*.json')):
        if path == skip:
            continue
        try:
            record = json.loads(path.read_text(encoding='utf-8-sig'))
            params = record['params']
            owner = params['job_id']
            mine = owner == job_id and path == journal_path(job_id, params['request_id'])
        except FileNotFoundError:
            continue  # reconciliation just retired it
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CronJournalCorrupt(path, f'unreadable ({type(exc).__name__}: {exc})') from exc
        if owner == job_id:
            if not mine:
                raise CronJournalCorrupt(path, 'identity conflict')
            yield path, record


def has_pending_admission(job_id):
    """An admission journal owns settlement, even after its firer died. Do not retire its job.

    Only a matching, identity-checked journal holds the job; unrelated journals do not. A damaged
    journal raises CronJournalCorrupt naming its path, so it cannot turn an uncertain fire into
    destructive cleanup; callers hold the job (``admission_holds``).
    """
    return next(_job_journals(job_id), None) is not None


def admission_holds(job, logger):
    """[daveey] True when an exhausted one-shot must be kept: a journal owns its settlement, or the
    journal check itself failed. A failure books nothing and blames the journal path (not the
    healthy job) so an operator can repair or remove that file; it never escapes to the caller's
    crash path (failure notice + booked failed run on a one-shot whose fire was already spent)."""
    try:
        return has_pending_admission(job['id'])
    except Exception as exc:
        where = getattr(exc, 'path', None) or journal_path(job['id'], '').parent
        logger.warning(
            "Job '%s': one-shot dispatch limit reached but admission journal %s could not be "
            "checked (%s) — holding the job unretired until that journal is repaired or reconciled",
            job.get('name') or job['id'], where, exc, exc_info=True)
        return True


def _pause_identity(paused):
    """The exact pause this recovery uncertainty caused, so only that pause is ever lifted."""
    return {'paused_at': paused.get('paused_at'), 'reason': paused.get('paused_reason')} if paused else True


def replayable_completions(job_id, completions):
    """The recorded completion ids whose admission journal still exists: only journal recovery
    can replay a completion into ``mark_job_run``, so an id without one is a dead tombstone."""
    return [eid for eid in completions or () if journal_path(job_id, eid).exists()]


def _sibling_pauses(job_id, execution_id):
    """Pause identities still needed by other journaled fires of this job."""
    for _path, record in _job_journals(job_id, skip=journal_path(job_id, execution_id)):
        yield record.get('pause_recorded')


def pause_for_unknown(job_id, execution_id, reason):
    """Record a shared uncertainty hold without replacing an operator's hold or disable."""
    from cron.jobs import _jobs_lock, get_job, is_job_runnable, pause_job
    from utils import atomic_json_write
    journal = journal_path(job_id, execution_id)
    with _jobs_lock():
        job = get_job(job_id)
        if job is not None and is_job_runnable(job):
            paused = _pause_identity(pause_job(job_id, reason=reason))
        else:
            current = job and _pause_identity(job)
            paused = current if current and current in list(_sibling_pauses(job_id, execution_id)) else True
        if journal.exists():
            record = json.loads(journal.read_text(encoding='utf-8-sig'))
            record.setdefault('pause_recorded', paused)
            atomic_json_write(journal, record, mode=0o600, fsync_dir=True)


def _lift_pause(job_id, pause, execution_id=None):
    """A verified terminal receipt resolved the uncertainty that paused the schedule: resume it iff
    the job is still held by exactly that pause (an operator's own pause/resume is never touched)."""
    from cron.jobs import _jobs_lock, get_job, resume_job
    with _jobs_lock():
        job = get_job(job_id) if isinstance(pause, dict) else None
        if (job is not None and job.get('state') == 'paused'
                and (job.get('paused_at'), job.get('paused_reason')) == (pause['paused_at'], pause['reason'])
                and pause not in list(_sibling_pauses(job_id, execution_id))):
            try:
                resume_job(job_id)
            except ValueError:  # a one-shot whose instant has passed stays paused for the operator
                pass


def retire_terminal_journal(job_id, execution_id):
    """Retire a terminal fire's uncertainty pause before discarding its recovery identity.

    Called only after terminal bookkeeping, never to replay output or retry the old fire.
    Keep the journal if lifting the pause fails so a later reconciliation can retry.
    """
    from cron.jobs import _jobs_lock
    journal = journal_path(job_id, execution_id)
    with _jobs_lock():
        try:
            record = json.loads(journal.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return
        params = record['params']
        if (params['job_id'], params['request_id']) != (job_id, execution_id):
            raise ValueError('cron journal identity conflict')
        _lift_pause(job_id, record.get('pause_recorded'), execution_id)
        journal.unlink(missing_ok=True)


def run_canonical_job(job, *, extra_prompt=None, cancel_event=None, execution_id=None):
    from gateway.session_cron import owner_for_home, operation
    from hermes_cli.gateway_client import connect_gateway
    from hermes_constants import get_hermes_home
    from utils import atomic_json_write

    request_id = execution_id or job.get('execution_id')
    if not request_id:
        request_id = job['execution_id'] = uuid.uuid4().hex
    params = {'job_id': job['id'], 'request_id': request_id,
              'extra_prompt': extra_prompt}
    root = get_hermes_home() / 'cron' / 'admissions'
    journal = journal_path(params['job_id'], params['request_id'])
    record = {'params': params, 'receipt': None}
    if journal.exists():
        record = json.loads(journal.read_text(encoding='utf-8-sig'))
        if record['params'] != params:
            raise CronExecutionUnknown('cron admission identity conflict')
    attempted = journal.exists()
    owner = owner_for_home(get_hermes_home())

    def write_journal():
        root.mkdir(parents=True, exist_ok=True)
        atomic_json_write(journal, record, mode=0o600, fsync_dir=True)

    async def save():
        # ``observe`` runs on the owner loop when this process owns the profile: the fsynced journal
        # write (file and directory) waits in a worker, and still completes before the next step.
        await asyncio.to_thread(write_journal)

    async def observe(call):
        nonlocal attempted
        recovering = attempted
        attempted = True
        await save()
        try:
            if recovering:
                state = await call('recover', params)
                if state['status'] not in {'queued', 'started', 'terminal'}:
                    raise CronExecutionUnknown('unknown_execution: cron admission was not recovered')
                receipt = {'session_id': state['session_id'], 'admission_id': state['admission_id']}
            else:
                receipt = await call('submit', params)
        except Exception as exc:
            from hermes_cli.gateway_client import GatewayRPCError
            # A returned owner exception is definite; a transport disconnect is not. Even an
            # owner error after commit must retain the admitted identity, so verify its absence.
            if recovering:
                raise
            if owner is not None or isinstance(exc, GatewayRPCError):
                from cron.restart_cut import not_ready_refusal
                refused = not_ready_refusal(exc)
                if refused:
                    # Durable before the absence check: if that check is lost too, journal
                    # recovery still knows the owner refused this fire as not ready.
                    record['submit_refused'] = refused
                    save()
                state = await call('recover', params)
                if state['status'] == 'missing' and refused:
                    # [daveey] Refused while draining for a restart and proven never admitted:
                    # nothing ran, so the fire is deferred to the next gateway, not failed.
                    journal.unlink(missing_ok=True)
                    attempted = False
                    raise CronFireNotAdmitted(
                        f'Not admitted: the gateway is draining ({refused}); the fire is deferred',
                        'draining', refused) from exc
                if state['status'] == 'missing':
                    error = f'{type(exc).__name__}: {exc}'
                    result = [False, f'# Cron Job: {job["id"]} (FAILED)\n\n{error}\n', '', error]
                    job['_fire_error'] = True  # refused before admission: the agent never ran
                    record['refusal'] = result
                    record['job'] = dict(job)
                    await save()
                    return tuple(result)
                if state['status'] not in {'queued', 'started', 'terminal'}:
                    raise
                # Admitted before the answer was lost: observe it through the recovered receipt.
                receipt = {'session_id': state['session_id'], 'admission_id': state['admission_id']}
            else:
                raise
        record['receipt'] = receipt
        await save()
        delay, progress = .1, None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                await call('cancel', receipt)
            state = await call('status', receipt)
            if state.get('progress_at') not in (None, progress):
                # The owner's agent is still working: refresh OUR ledger row (the owner's stamp is
                # fenced to the row's owner), once per owner stamp, so the stale-claim sweep measures
                # the run's silence. A wedged owner stops stamping and the sweep still fires.
                from cron.executions import touch_execution_progress
                progress = state['progress_at']
                await asyncio.to_thread(touch_execution_progress, request_id)
            if state['status'] == 'terminal':
                job.update(state.get('job_flags') or {})
                return tuple(state['result'])
            if state['status'] == 'unknown':
                raise CronExecutionUnknown('unknown_execution: cron admission was not replayed')
            await asyncio.sleep(delay)
            delay = min(delay * 2, 2.0)

    async def remote():
        from cron.observer_reconnect import observe_remote
        return await observe_remote(connect_gateway, observe, lambda: attempted)

    try:
        if owner is not None:
            authority, loop = owner
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if current is loop:
                raise RuntimeError('cron synchronous execution must run off the owner event loop')
            return asyncio.run_coroutine_threadsafe(
                observe(lambda op, data: operation(authority, op, data)), loop).result()
        return asyncio.run(remote())
    except Exception as exc:
        if isinstance(exc, CronFireNotAdmitted):
            raise
        if attempted:
            raise CronExecutionUnknown(f'Cron execution unverified; reconcile {journal}: {exc}') from exc
        from hermes_cli.gateway_client import GatewayUnavailableError
        if isinstance(exc, GatewayUnavailableError) and exc.state in _NOT_READY_STATES:
            # The gate's probe said ready, then an update fenced the gateway before connect:
            # nothing was submitted, so the fire is held for the next tick, not failed.
            reason = exc.state + (f' ({exc.reason_code})' if exc.reason_code else '')
            raise CronFireNotAdmitted(
                f'Not admitted: the gateway is {reason}; the fire stays due: {exc}',
                exc.state, exc.reason_code) from exc
        if job.get('_scheduled_instant') and _dial_failed(exc) and _connect_holds_left(job['id']):
            # The socket never opened (a stalled or restarting owner loop): nothing was submitted,
            # so a scheduled fire is held like a not-ready gateway (a manual run has no slot to keep
            # due and fails loudly). After CONNECT_HOLD_LIMIT consecutive
            # holds it is booked FAILED below, so a gateway that stays unreachable still alerts.
            from cron.unadmitted_fire import CONNECT_FAILED
            raise CronFireNotAdmitted(
                f'Not admitted: the gateway could not be reached ({CONNECT_FAILED}); the fire stays due: {exc}',
                'unreachable', CONNECT_FAILED) from exc
        error = f'{type(exc).__name__}: {exc}'
        job['_fire_error'] = True  # nothing was submitted: a fire/infra failure, not an agent verdict
        return False, f'# Cron Job: {job["id"]} (FAILED)\n\n{error}\n', '', error

def reconcile_pending(*, allow_connect=True):
    """Observe prepared fires; never submit missing or interrupted work.

    With allow_connect=False, a headless tick leaves durable receipts for the next
    live owner instead of ensuring or spawning a gateway.
    """
    from gateway.session_cron import owner_for_home, operation
    from hermes_constants import get_hermes_home
    from hermes_cli.gateway_client import connect_gateway
    from cron.executions import FIRER_FINISHED_STATES, execution_owner_live, get_execution
    from cron.jobs import get_job
    from cron.scheduler import (_RunDelivery, _FireOwnership, _apply_agent_failure_marker,
                                _save_compose_deliver)
    from cron.scheduler_bookkeeping import fail_empty_response, finish_completed_run
    import logging

    root = get_hermes_home() / 'cron' / 'admissions'
    for journal in sorted(root.glob('*.json')):
        try:
            record = json.loads(journal.read_text(encoding='utf-8-sig'))
            params = record['params']
            if journal != journal_path(params['job_id'], params['request_id']):
                raise ValueError('cron journal identity conflict')
            if execution_owner_live(params['request_id'], params['job_id']):
                # Its firer (this process included) still owns the row and its tail: re-saving the
                # output or re-marking the job on every tick would duplicate its side effects.
                continue
            row = get_execution(params['request_id'])
            if row is not None and row['job_id'] == params['job_id'] and row['status'] in FIRER_FINISHED_STATES:
                # The firer booked this attempt's terminal outcome itself (ownership lost, shutdown
                # interrupt, a crashed tail): lift only its own pause, never save or deliver again.
                retire_terminal_journal(params['job_id'], params['request_id'])
                continue
            if 'refusal' in record:
                state = {'status': 'terminal', 'result': record['refusal'], 'job': record['job']}
            else:
                owner = owner_for_home(get_hermes_home())
                if owner is None and not allow_connect:
                    continue
                async def observe():
                    if owner is not None:
                        return await operation(owner[0], 'recover', params)
                    async with connect_gateway() as client:
                        return await client.rpc('cron.recover', **params)
                if owner is not None:
                    state = asyncio.run_coroutine_threadsafe(observe(), owner[1]).result(timeout=20)
                else:
                    state = asyncio.run(observe())
            if state['status'] == 'missing':
                # The owner holds no admission for this fire: a definite non-admission, booked as
                # the same failed run the in-submit refusal books. Only ``unknown`` pauses.
                current = get_job(params['job_id'])
                if current is None:
                    journal.unlink(missing_ok=True)
                    continue
                from cron.restart_cut import NOT_READY_REFUSALS, defer_unadmitted
                if record.get('submit_refused') in NOT_READY_REFUSALS:
                    # [daveey] Refused while the owner drained for a restart: nothing ran, so the
                    # fire is deferred to this (the next) gateway instead of failed.
                    defer_unadmitted(current, params['request_id'], record['submit_refused'], None,
                                     recovered=True)
                    continue
                error = 'Cron admission missing: the owner never admitted this fire'
                state = {'status': 'terminal', 'job': current, 'job_flags': {'_fire_error': True},
                         'result': [False, f'# Cron Job: {params["job_id"]} (FAILED)\n\n{error}\n', '', error]}
            if state['status'] != 'terminal':
                if state['status'] == 'unknown' and not record.get('pause_recorded'):
                    pause_for_unknown(params['job_id'], params['request_id'],
                                      'Canonical cron unknown; no automatic re-execution')
                continue
            success, output, answer, error = state['result']
            job = dict(state['job'], **(state.get('job_flags') or {}))
            job['execution_id'] = params['request_id']
            from cron.restart_cut import RESTART_CUT_FLAG, settle_restart_cut
            if not success and job.pop(RESTART_CUT_FLAG, False):
                # [daveey] The owner's restart cut this run: an interruption rerun once, not a failure.
                settle_restart_cut(job, params['request_id'], error, None, recovered=True)
                continue
            # The same result verdicts as the ordinary tail: an agent-declared [CRON_FAILURE] and a
            # blank answer are failures, whichever process books them.
            success, error, declared = _apply_agent_failure_marker(job, success, error, answer)
            delivery = _RunDelivery(job, success, error, agent_declared=declared)
            # The canonical receipt supplies the result; a dead scheduler's fire claim does not
            # authorize replay and must not prevent finishing its durable bookkeeping.
            fence = _FireOwnership(dict(job, fire_claim=None))
            _save_compose_deliver(delivery, fence, answer, output, adapters=None, loop=None,
                                 verbose=False, execution_token=None)
            fail_empty_response(delivery, answer)
            finish_completed_run(delivery, None, params['request_id'], recovered=True)
        except Exception:
            logging.getLogger(__name__).warning('Cron receipt recovery deferred: %s', journal, exc_info=True)
