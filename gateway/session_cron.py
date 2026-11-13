"""Trusted cron producer and owner execution of the existing scheduler contract.

Public callers name a persisted job, never supply agent policy or arbitrary jobs.
The ordinary admission ledger owns execution; the scheduler owns final delivery.
"""
import asyncio
from contextvars import ContextVar
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import threading
import time

from gateway.session_contract import SessionActor, SessionRef
from hermes_state_keys import CRON_PRINCIPAL_ID, cron_request_id
from hermes_state_runtime import RuntimeStoreError, admit_session_input, get_session_admission

_execution: ContextVar[tuple | None] = ContextVar('cron_owner_execution', default=None)
_owners = {}


def bind_owner(authority):
    home = Path(authority.db.db_path).resolve().parent
    _owners[home] = (os.getpid(), authority, asyncio.get_running_loop())


def unbind_owner(authority):
    home = Path(authority.db.db_path).resolve().parent
    bound = _owners.get(home)
    if bound is not None and bound[1] is authority:
        del _owners[home]


def owner_for_home(home):
    bound = _owners.get(Path(home).resolve())
    if bound is not None and bound[0] == os.getpid() and not bound[2].is_closed():
        return bound[1:]
    return None


async def _admit_local_delivery(authority, chat_id, text, metadata):
    from gateway.config import Platform
    from gateway.session_authorities import owner_scope
    from gateway.session_automation import admit_route_wake
    from gateway.session_local_recovery import local_adapter_map, restore_local_session
    from gateway.session_route_recovery import mountable, restore_routed_session
    with owner_scope(authority):
        live = authority.sessions.get(chat_id)
        if live is None and mountable(authority, chat_id):
            # A messaging conversation a reminder set from the app names (gateway/session_detached.py),
            # not mounted (a restart): it mounts off the chat, or on it when the chat names it again.
            restore_routed_session(authority, chat_id)
        elif live is None or live.source is None or live.source.platform == Platform.LOCAL:
            restore_local_session(authority, chat_id)  # a chat closed before this fire, as for completions
        source = authority.sessions[chat_id].source
        # Off the chat its app viewers take it (as a note); back on the chat (its /resume) it is that
        # conversation's own turn there.
        adapter = (local_adapter_map(authority)[Platform.LOCAL] if source.platform == Platform.LOCAL
                   else authority.runner._adapter_for_source(source))
        return await admit_route_wake(authority.runner, adapter, source, text, 'cron', metadata)


def deliver_to_local_chat(home, chat_id, text, *, diagnostic=False):
    """Admit a cron result into local chat *chat_id*'s FIFO; at the head it is written into the chat's
    history as a note, never a turn (``session_completion_notes``). A local chat has no push transport
    (``LocalSessionAdapter.send`` only acknowledges a turn's own output), so only the owner in this
    process can deliver. Called off the owner loop; None once admitted, else the error."""
    owner = owner_for_home(home)
    if owner is None:
        return f"local chat {chat_id}: no gateway in this process owns its session"
    authority, loop = owner
    metadata = {'notification_category': 'diagnostic'} if diagnostic else {}
    try:
        admitted = asyncio.run_coroutine_threadsafe(
            _admit_local_delivery(authority, chat_id, text, metadata), loop).result(timeout=60)
    except (RuntimeStoreError, KeyError, TimeoutError) as exc:  # unknown chat, refused admission, stalled owner
        return f"local chat {chat_id}: delivery not confirmed: {exc!r}"
    return None if admitted else f"local chat {chat_id}: delivery refused: the chat has no committed route"


def current_execution():
    return _execution.get()


def bind_execution(authority, session_id, job_id, execution_id):
    """Bind this context's cron admission ``(authority, session_id, job_id, execution_id)``, the
    layout ``current_execution()`` readers index; returns the ``ContextVar`` reset token."""
    return _execution.set((authority, session_id, job_id, execution_id))


def _actor(authority, actor):
    if actor is None:
        return SessionActor(CRON_PRINCIPAL_ID, authority.profile_id,
                         frozenset({'session:create', 'session:submit', 'session:read', 'session:control'}), 'cron-ticker')
    if actor.profile_id != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    if not {'session:create', 'session:submit', 'session:control'} <= actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    # Cron fires belong to the profile scheduler, not the transport that observes
    # them. The same execution ID from ticker and CLI must dedupe together.
    return SessionActor(CRON_PRINCIPAL_ID, authority.profile_id, actor.capabilities, actor.transport_id)


def _create(authority, actor, params):
    from cron.jobs import get_job
    from gateway.session_local_recovery import local_identity, restore_local_session
    from gateway.session_policy import build_policy, bind_launch_key
    from gateway.run import _load_gateway_config
    from cron.scheduler import _load_cron_job_config
    from gateway.config import Platform
    from gateway.session import SessionSource, SessionEntry
    from gateway.session_lifecycle import _now

    if set(params) != {'job_id', 'request_id', 'extra_prompt'}:
        raise RuntimeStoreError('invalid_params')
    jid, request_id, extra = params['job_id'], params['request_id'], params['extra_prompt']
    if (not isinstance(jid, str) or not jid or not isinstance(request_id, str)
            or not request_id or len(request_id) > 256 or (extra is not None and not isinstance(extra, str))):
        raise RuntimeStoreError('invalid_params')
    execution_id, request_id = request_id, cron_request_id(jid, request_id)
    sid = local_identity(authority.profile_id, actor.subject, request_id)
    if authority.db.get_session(sid) is not None:
        return restore_local_session(authority, sid), request_id
    if ':' in execution_id:
        # Fresh admissions only: ``cron:<job>:<execution>`` then splits at its one ``:`` after the
        # job. Ledger ids are hex, so only a hand-made id has one; an identity admitted before this
        # guard keeps its exact retry above, and run history files it by its stored policy.
        raise RuntimeStoreError('invalid_params')
    job = get_job(jid)
    if job is None or job.get('no_agent'):
        raise RuntimeStoreError('not_found')
    config = _load_gateway_config()
    private = {}
    launch = {'source': 'cli', 'request_id': request_id, 'toolsets': []}
    if job.get('workdir'):
        launch['cwd'] = job['workdir']
    policy = build_policy(launch, config, private_secrets=private)
    policy = replace(policy, source='cron', platform='cron', model=_load_cron_job_config(job, jid, job.get('name') or jid).model,
                     request_json=json.dumps({'cron_job': job, 'extra_prompt': extra, 'request_id': request_id}))
    policy = bind_launch_key(authority, sid, policy, None, config_secrets=private)
    from gateway.session_local_recovery import local_source
    source = local_source(authority, sid, actor.subject)
    route = authority.runner.session_store._generate_session_key(source)
    now = _now()
    entry = SessionEntry(route, sid, now, now, origin=source, platform=Platform.LOCAL)
    # The staged creation receipt: ``operation('submit')`` commits it off the owner loop.
    return {'profile_id': authority.profile_id, 'principal_id': actor.subject, 'request_id': request_id,
            'session_id': sid, 'route': route, 'entry': entry.to_dict(), 'policy': asdict(policy)}, request_id


# What a message typed into a scheduled job's session settles with (see ``execute``); its text is
# ``gateway.errors.cron_session_read_only``, rendered in the owning profile's language.
CRON_SESSION_READ_ONLY_SURFACE = {'layer': 'gateway', 'code': 'cron_session_read_only', 'retryable': False}

# Run-side verdicts run_job stamps on the job dict for the firing scheduler's bookkeeping tail
# (cron/quota_hold.py parks past a closed usage window; cron/unreachable_retry.py re-runs a
# never-reached model). They cross the authority boundary inside the cron result.
# [daveey] ``_agent_skipped``: a wake gate or unchanged monitor suppressed the agent, so the tick
# keeps the job's last real verdict (failure streak, alert-once marker) instead of resetting it.
# [daveey] ``_gate_error``: a pre-agent gate (monitor source, unparseable config) failed; booked as
# ``last_error_kind = "gate"`` so the next good gate tick may clear the streak.
RUN_JOB_FLAGS = ('_model_unreachable', '_quota_hold_seconds', '_quota_hold_route', '_agent_skipped', '_gate_error')


async def operation(authority, name, params, actor=None):
    actor = _actor(authority, actor)
    from gateway.session_authorities import require_serving
    require_serving(authority)  # released ownership: nothing here is ours to touch
    # Only new work is fenced by a drain. ``recover``/``status``/``cancel`` observe or stop work
    # already admitted, which the drain is waiting for: refusing them makes the firer give up
    # (CronExecutionUnknown pauses the job) on every restart that overlaps a cron run.
    if name == 'recover':
        from gateway.session_local_recovery import local_identity
        if (set(params) != {'job_id', 'request_id', 'extra_prompt'}
                or any(not isinstance(params[k], str) or not params[k] for k in ('job_id', 'request_id'))
                or (params['extra_prompt'] is not None and not isinstance(params['extra_prompt'], str))):
            raise RuntimeStoreError('invalid_params')
        request_id = cron_request_id(params['job_id'], params['request_id'])
        sid = local_identity(authority.profile_id, actor.subject, request_id)
        from hermes_state_runtime import list_session_admissions
        rows = list_session_admissions(authority.db, session_id=sid, pending_only=False)
        from gateway.owner_identity import same_owner
        row = next((r for r in rows if r['request_id'] == request_id and same_owner(r['principal_id'], actor.subject)), None)
        if row is None:
            return {'status': 'missing', 'result': None}
        state = await operation(authority, 'status', {'session_id': sid, 'admission_id': row['admission_id']}, actor)
        from gateway.session_local_recovery import restore_local_session
        restore_local_session(authority, sid)
        from gateway.config import Platform
        from gateway.session_local_recovery import local_adapter_map
        policy = local_adapter_map(authority)[Platform.LOCAL].policies[sid]
        frozen = json.loads(policy.request_json)
        if frozen['extra_prompt'] != params['extra_prompt']:
            raise RuntimeStoreError('admission_conflict')
        state['job'] = frozen['cron_job']
        # The receipt a firer whose submit answer was lost polls, exactly as if it had arrived.
        state.update(session_id=sid, admission_id=row['admission_id'])
        return state
    if name == 'submit':
        authority._require_admission_open()
        from gateway.session_authorities import owner_scope
        # The job id, jobs file and gateway config belong to the OWNING profile.
        with owner_scope(authority):
            ref, request_id = _create(authority, actor, params)
        from functools import partial
        from gateway.session_runtime_workers import tracked_write
        if isinstance(ref, dict):
            # The creation commit waits on the writer off the owner loop, like session.create (N50).
            from gateway.session_local_recovery import restore_local_session
            from hermes_state_local import commit_local_session
            await tracked_write(authority, partial(commit_local_session, authority.db, epoch=authority.epoch, receipt=ref))
            ref = restore_local_session(authority, ref['session_id'])
        receipt = await tracked_write(authority, partial(
            admit_session_input, authority.db, epoch=authority.epoch, principal_id=actor.subject,
            session_id=ref.session_id, request_id=request_id, payload={'text': params['extra_prompt'] or ''},
            _authorize_write=authority._admission_gate()), then=partial(authority._admitted, ref, None), ordered=True,
            after=partial(authority._schedule_admitted, ref))
        return {'session_id': ref.session_id, 'admission_id': receipt.admission_id}
    if set(params) != {'session_id', 'admission_id'} or name not in {'status', 'cancel'}:
        raise RuntimeStoreError('invalid_params')
    ref = SessionRef(authority.profile_id, params['session_id'])
    authority.authorize(actor, ref, 'session:submit')
    row = get_session_admission(authority.db, admission_id=params['admission_id'])
    from gateway.owner_identity import same_owner
    if row is None or row['target_session_id'] != ref.session_id or not same_owner(row['principal_id'], actor.subject):
        raise RuntimeStoreError('permission_denied')
    if name == 'cancel':
        if row['status'] == 'queued':
            row = await authority.cancel_or_claimed(actor, ref, row['admission_id']) or {'status': 'terminal'}
        if row['status'] == 'started':
            # The claim commits before execute() registers its event; latching the
            # cancellation on the admission here means that window cannot lose it.
            latch_stop(authority, _cancellations(authority).setdefault(row['admission_id'], threading.Event()))
        return {'ok': True}
    result, flags = None, {}
    if row['status'] == 'terminal':
        from gateway.session_results import admission_result
        saved = admission_result(authority.db, row['admission_id'])
        result = saved['result'].get('cron_result') if saved else None
        flags = (saved['result'].get('cron_job_flags') or {}) if saved else {}
        if result is None:
            result = [False, '', '', row.get('outcome') or 'unknown_execution']
            if row.get('outcome') == 'interrupted' and saved is None:
                # [daveey] The run's turn was lost across this owner's restart (boot recovery
                # settles an unattended ``unknown`` admission interrupted, with no result): a
                # restart cut, rerun once by whoever books it (cron/restart_cut.py), not a failure.
                from cron.restart_cut import RESTART_CUT_FLAG
                flags = dict(flags, **{RESTART_CUT_FLAG: True})
    from cron.scheduler_liveness import owner_progress
    return {'status': row['status'], 'result': result, 'job_flags': flags,
            'progress_at': owner_progress.get(row['request_id'].rsplit(':', 1)[-1])}


def _cancellations(authority):
    cancellations = getattr(authority, '_cron_cancellations', None)
    if cancellations is None:
        cancellations = authority._cron_cancellations = {}
    return cancellations


def _owner_draining(authority):
    runner = getattr(authority, 'runner', None)
    return bool(getattr(runner, '_draining', False) or getattr(runner, '_interrupting_remaining_work', False))


def latch_stop(authority, cancel):
    """[daveey] Set a run's cancel latch, recording on it whether this owner's own drain/restart is
    what stopped it. The fact is latched at stop time because the run thread reads it up to a poll
    later, after the shutdown's last phase has reset ``runner._draining`` (cron-7c04faf754: the
    run then reported "cron fire ownership was lost" and was booked failed). The first stop wins:
    a latch already set by another stop (a real takeover's forwarded cancel) keeps its meaning.

    This is permanent for the run, even if the restart-wait drain is aborted and the owner keeps
    serving. A real lost-claim cancel forwarded during that drain is indistinguishable from a
    shutdown cancel: prefer the safe-side single restart-cut rerun over booking a failure."""
    if not cancel.is_set() and _owner_draining(authority):
        cancel.owner_shutdown = True
    cancel.set()


def _latched_owner_shutdown(cancel, execution_id):
    """Whether *cancel* was set by this owner's drain/restart; an operator's cancel marker wins."""
    from cron.cancellation import cancel_requested
    return (cancel is not None and cancel.is_set() and bool(getattr(cancel, 'owner_shutdown', False))
            and not cancel_requested(execution_id))


def _restart_cut(authority, stop, execution_id, cancel=None):
    """[daveey] Whether this owner's own restart stopped the run: the latch says its drain stopped
    it (``latch_stop``), or it is still draining for a stop or an update and has either begun
    interrupting remaining work (tools killed, agents interrupted) or the run was torn down by
    something other than an operator's cancel (the shutdown cleared the fire claim, so the firer
    forwarded a "lost ownership" cancel)."""
    from cron.cancellation import cancel_requested
    if _latched_owner_shutdown(cancel, execution_id):
        return True
    runner = getattr(authority, 'runner', None)
    # Sticky for this process, read first: a run interrupted by the stop can unwind after stop()
    # finished and reset ``_draining`` (the run outlives the executor quiesce).
    if getattr(runner, '_interrupting_remaining_work', False):
        return True
    if not getattr(runner, '_draining', False):
        return False
    return bool(stop is not None and stop.is_set() and not cancel_requested(execution_id))


async def rpc(connection, name, params):
    return await operation(connection.authority, name, params, connection.actor)


def scheduler_execution_id(job_id, request_id):
    """The scheduler's execution id this admission carries: ``_create`` admits under
    ``cron:<job>:<request_id>`` and the ticker's ``request_id`` IS its ``cron/executions`` row id."""
    return request_id.removeprefix(cron_request_id(job_id, ''))


def _run_identified(run_job, job, execution_id, **kwargs):
    """``run_job`` inside this fire's cron identity (``ctx.current_cron_execution()``).

    The scheduler binds it in the FIRING thread, but the owner executes on its own loop, whose
    context never saw that binding; re-bind it here from the same ledger row, in the profile scope
    the caller entered (so the row and the profile name are this profile's)."""
    from cron.execution_identity import enter_cron_execution, exit_cron_execution
    from cron.executions import get_execution
    token = enter_cron_execution(job, execution_id, get_execution(execution_id) or {})
    try:
        return run_job(job, execution_id=execution_id, **kwargs)
    finally:
        exit_cron_execution(token)


class CronTurnStopped(RuntimeError):
    """A cron turn that ended in a stop someone asked for (CronRunCancelled) or in this gateway's own
    shutdown (CronShutdownInterrupted). ``run_job`` already logged it as one WARNING; the authority
    still settles the turn ``failed`` but logs it without the ERROR traceback (cron-7c04faf754)."""
    expected_stop = True


def _expected_stop(error):
    """Whether ``run_job``'s error is one of its expected stops, read from the ``<type>: <message>``
    text it builds (``scheduler._job_error_message``) for exactly the exceptions ``_log_expected_stop``
    logs as a WARNING."""
    from cron.cancellation import CronRunCancelled, CronShutdownInterrupted
    return isinstance(error, str) and error.startswith(
        tuple(f'{cls.__name__}: ' for cls in (CronRunCancelled, CronShutdownInterrupted)))


async def execute(authority, ref, row, policy):
    from cron.scheduler import run_job
    from gateway.run import _profile_runtime_scope
    data = json.loads(policy.request_json)
    if (row['payload'] != {'text': data['extra_prompt'] or ''}
            or row['request_id'] != data['request_id']
            or row['principal_id'] != authority.sessions[ref.session_id].source.user_id):
        # A cron session only runs its own frozen fire; anything else typed into it fails with a
        # descriptor the viewer can render (and offer a new chat for), never a generic failure.
        from agent.i18n import t
        text = t('gateway.errors.cron_session_read_only')
        authority.pending_results[row['admission_id']] = {'result': {
            'final_response': '', 'messages': [], 'failed': True, 'completed': False,
            'error': text, 'error_surface': dict(CRON_SESSION_READ_ONLY_SURFACE)},
            'usage': {}}
        return text
    cancellations = _cancellations(authority)
    cancel = cancellations.setdefault(row['admission_id'], threading.Event())
    execution_id = scheduler_execution_id(data['cron_job']['id'], row['request_id'])
    # The frozen snapshot predates the fire's ledger row; the run's progress stamps need its id.
    job = dict(data['cron_job'], execution_id=execution_id)
    token = bind_execution(authority, ref.session_id, job['id'], execution_id)
    try:
        from cron.cancellation import CancelRequest, ShutdownAwareCancel, _CombinedCancelEvent
        from cron.scheduler import fire_interrupted_by_shutdown

        db_path = await asyncio.to_thread(Path(authority.db.db_path).resolve)
        from agent.runtime_scope import bind_turn, claim_fence
        with _profile_runtime_scope(db_path.parent), bind_turn(ref.session_id, row['admission_id'], claim_fence(row)):
            # The forwarded ``cron.cancel`` carries no reason, so the operator's marker is read here
            # first: a stopped agent run reports "cancelled on request", not a lost fire claim. When
            # the firer is this process and its shutdown interrupted the fire, the forwarded cancel
            # reports that shutdown, not lost ownership (cron-7c04faf754). So does a stop while this
            # owner drains for a restart with no operator marker: ``stop_authority_work`` latches the
            # run's cancel without a reason and without the scheduler's interrupted mark, but with
            # ``owner_shutdown`` recorded at stop time (``latch_stop``), since the drain flag is reset
            # before the run's next poll. An external (restart-safe) firer's interrupted mark lives
            # in its own process, so only the latch can tell this owner's run about the shutdown.
            job_id = job['id']
            from cron.cancellation import cancel_requested

            def _stopped_by_own_shutdown():
                if fire_interrupted_by_shutdown(job_id) or _latched_owner_shutdown(cancel, execution_id):
                    return True
                if cancel_requested(execution_id):
                    return False
                # The interrupt latch outlives stop()'s ``_draining`` reset (see _restart_cut).
                return _owner_draining(authority)

            stop = _CombinedCancelEvent(
                CancelRequest(execution_id),
                ShutdownAwareCancel(cancel, _stopped_by_own_shutdown))
            # [daveey] The attempt's clock: monotonic stops in host sleep, wall time does not.
            started_awake, started_wall = time.monotonic(), time.time()
            try:
                result = await asyncio.to_thread(_run_identified, run_job, job, execution_id,
                                                 extra_prompt=data['extra_prompt'], cancel_event=stop)
            except Exception as exc:
                if not _restart_cut(authority, stop, execution_id, cancel):
                    raise
                result = (False, '', '', f'{type(exc).__name__}: {exc}')
        # run_job stamps run-side verdicts on ITS job copy (quota hold, unreachable model); the
        # firing scheduler's bookkeeping reads them off its own dict, so they ride the result.
        flags = {key: job[key] for key in RUN_JOB_FLAGS if key in job}
        if not result[0] and _restart_cut(authority, stop, execution_id, cancel):
            # [daveey] This owner's restart stopped the run (killed its tools, interrupted its agent,
            # or cleared its claim): whatever it returned is not the job's verdict. The firer books
            # an interruption and reruns it once (cron/restart_cut.py), never a failure.
            from cron.restart_cut import AWAKE_FLAG, RESTART_CUT_FLAG, RESTART_CUT_REASON
            flags[RESTART_CUT_FLAG] = True
            # How long the host was awake for it: a rerun the host slept through is re-armed.
            flags[AWAKE_FLAG] = {'awake': round(time.monotonic() - started_awake, 1),
                                 'wall': round(max(0.0, time.time() - started_wall), 1)}
            result = (False, result[1], '', result[3] or RESTART_CUT_REASON)
        authority.pending_results[row['admission_id']] = {
            'result': {'final_response': result[2], 'cron_result': list(result), 'cron_job_flags': flags,
                       'failed': not result[0], 'completed': result[0]}, 'usage': {}}
        if not result[0]:
            if _expected_stop(result[3]):
                raise CronTurnStopped(result[3])
            raise RuntimeError(result[3] or 'cron execution failed')
        return result[2]
    finally:
        from cron.scheduler_liveness import owner_progress
        owner_progress.pop(execution_id, None)
        _execution.reset(token)
        cancellations.pop(row['admission_id'], None)
