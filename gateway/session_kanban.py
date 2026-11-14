"""Trusted dispatcher claims enter the ordinary owner's durable admission queue."""
import asyncio
import contextlib
from contextlib import closing
from dataclasses import asdict, replace
import threading
import json
from pathlib import Path
import sqlite3

from hermes_state_runtime import RuntimeStoreError


def build_kanban_policy(connection, params, config):
    from hermes_cli import kanban_db as kb
    from hermes_cli.profiles import resolve_profile_env
    from gateway.session_policy import build_policy
    authority, actor = connection.authority, connection.actor
    if not connection.native_owner or 'session:create' not in actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    if actor.profile_id != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    if (set(params) not in ({'board', 'task_id', 'run_id', 'claim_lock'}, {'board', 'task_id', 'run_id', 'claim_lock', 'db'})
            or any(not isinstance(params[k], str) or not params[k] for k in ('board', 'task_id', 'claim_lock'))
            or type(params['run_id']) is not int):
        raise RuntimeStoreError('invalid_params')
    try:
        board = kb._require_slug(params['board'])
        path = Path(params['db']).resolve(strict=True) if 'db' in params else kb.kanban_db_path(board=board).resolve()
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            task = kb.get_task(conn, params['task_id'])
            run = conn.execute('SELECT * FROM task_runs WHERE id=?', (params['run_id'],)).fetchone()
            if (task is None or run is None or task.status != 'running' or run['status'] != 'running'
                    or task.current_run_id != params['run_id'] or run['task_id'] != task.id
                    or task.claim_lock != params['claim_lock'] or run['claim_lock'] != task.claim_lock):
                raise RuntimeStoreError('stale_kanban_claim')
            if Path(resolve_profile_env(task.assignee)).resolve() != Path(authority.profile_id).resolve():
                raise RuntimeStoreError('profile_mismatch')
            bound = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='owner_admitted' ORDER BY id DESC LIMIT 1",
                                 (task.id, task.current_run_id)).fetchone()
            if bound and json.loads(bound[0])['request_id'] != 'kanban:' + json.dumps([board, task.id, task.current_run_id], separators=(',', ':')):
                raise RuntimeStoreError('stale_kanban_claim')
            context = kb.build_worker_context(conn, task.id)
            # [daveey] The run before this one lost to an owner restart: resume its conversation.
            from gateway.session_kanban_resume import restart_candidate
            resume_from = restart_candidate(conn, task.id, task.current_run_id)
            resume_base = kb.build_worker_context(conn, task.id, recovery_tail=False) if resume_from else None
            from hermes_cli.kanban_db_context import ctx_workspace_span
            context_workspace = ctx_workspace_span(task)  # [daveey] the capsule rewrite's exact header span
            # [daveey] Owner-derived (task row + resolved backend), never caller/config supplied.
            from gateway.session_kanban_capsule import capsule_requirement
            capsule = capsule_requirement(task)
            # [daveey] The dispatcher's model tier for this run (in-memory pin + model_tier event), class → auto.
            from hermes_cli.kanban_model_tier import owner_model_pin
            pinned_model, pinned_provider = owner_model_pin(conn, task)
            skills = list(task.skills or ())
            if kb._retry_status_for_run(conn, task.id, task.current_run_id) == 'review':
                # Same rule as the dispatcher's review lane: the reviewer always loads sdlc-review.
                skills = list(dict.fromkeys([*skills, 'sdlc-review']))
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise RuntimeStoreError('invalid_kanban_claim') from exc
    cwd = task.workspace_path
    params_policy = {'source': 'cli', 'cwd': cwd}
    for name, value in (('model', pinned_model), ('provider', pinned_provider), ('reasoning', task.reasoning_effort)):
        if value:
            params_policy[name] = value
    private = {}
    policy = build_policy(params_policy, config, private_secrets=private)
    if policy.model is None:
        from gateway.run import _resolve_gateway_model
        policy = replace(policy, model=_resolve_gateway_model(policy.config()))
    context = dict(params, workspace=cwd, branch=task.branch_name, tenant=task.tenant,
        profile=task.assignee, workspaces_root=str(kb.workspaces_root(board=board).resolve()),
        skills=skills, goal_mode=task.goal_mode, goal_max_turns=task.goal_max_turns,
        goal_text='\n\n'.join(p for p in (task.title, task.body) if p), context=context,
        context_workspace=context_workspace,
        max_runtime_seconds=task.max_runtime_seconds, accept_hooks=True, capsule=capsule)
    context['db'] = str(path)
    if resume_from is not None:  # [daveey] planned and copied off-loop in run_task, after session creation
        from gateway.session_kanban_resume import resume_context
        context['restart_candidate'] = {**resume_from, 'assignee': task.assignee}
        context['resume_context'] = resume_context(resume_base, {'prior_run_id': resume_from['prior_run_id'],
                                                                 'run_id': task.current_run_id})
        from gateway.session_kanban_capsule import resume_workspace_span
        context['resume_context_workspace'] = resume_workspace_span(context, resume_base)
    request = json.loads(policy.request_json)
    request.update(source='kanban', board=board, task_id=task.id, run_id=task.current_run_id)
    return replace(policy, source='kanban', platform='cli', toolsets=tuple(sorted(set(policy.toolsets) | {'kanban'})),
                   request_json=json.dumps(request, sort_keys=True), kanban_json=json.dumps(context)), private


async def run_task(connection, params):
    if not connection.native_owner or 'session:create' not in connection.actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    from gateway.run import _load_gateway_config
    from gateway.session_local import create_local_session
    from gateway.session_contract import Submission
    from gateway.session_local_recovery import local_identity, _restore_receipt_policy
    from hermes_state_local import local_receipt
    from gateway.session_kanban_capsule import require_capsule_transport
    authority = connection.authority
    # Resolved off-loop up front: no await separates the saved-claim check from session
    # creation. The Kanban claim check and its marker share one off-loop transaction; a
    # concurrent retry of the same attempt meets that marker (same session) or a stale claim.
    owner_db = str(await asyncio.to_thread(Path(authority.db.db_path).resolve))
    # The dispatcher attempt is the durable request identity, including after the
    # task closes. Retry checks the saved claim, never reinterprets updated cards.
    request_id = 'kanban:' + json.dumps([params.get('board'), params.get('task_id'), params.get('run_id')], separators=(',', ':'))
    sid = local_identity(authority.profile_id, connection.actor.subject, request_id)
    saved = local_receipt(authority.db, sid) if authority.db.get_session(sid) is not None else None
    if saved:
        policy = _restore_receipt_policy(authority, sid, saved)
        context = json.loads(policy.kanban_json or '{}')
        if (not connection.native_owner or set(params) not in ({'board', 'task_id', 'run_id', 'claim_lock'}, {'board', 'task_id', 'run_id', 'claim_lock', 'db'})
                or any(params[k] != context.get(k) for k in params)):
            raise RuntimeStoreError('permission_denied')
        # [daveey] Replayed/old saved policy: re-derived from the board before any resume/admission.
        await asyncio.to_thread(require_capsule_transport, policy)
        from gateway.session_local_recovery import restore_local_session
        ref = restore_local_session(authority, sid)
    else:
        policy, private = build_kanban_policy(connection, params, _load_gateway_config())
        # [daveey] Capsule-required card without live run-bound transport refuses before session creation.
        await asyncio.to_thread(require_capsule_transport, policy)
        ref = create_local_session(authority, connection.actor, {'request_id': request_id},
                                   trusted_policy=policy, trusted_secrets=private)
    candidate = json.loads(policy.kanban_json or '{}').get('restart_candidate') if not saved else None
    resume_plan, copied = await _seed_restart_resume(authority, ref.session_id, policy, candidate, params['run_id'])
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli import kanban_db as kb
    import os
    import psutil
    from hermes_cli.process_identity import worker_birth
    context = json.loads(policy.kanban_json)

    def admit_claim():
        # The Kanban DB is the cross-profile dispatch bus (busy_timeout up to 120 s): the claim
        # check and its marker share one write transaction, off the owner loop.
        with connect_closing(Path(context['db'])) as conn, kb.write_txn(conn):
            # Either this exact attempt was already admitted to this session (retry after a
            # crash between session creation and admission), or the claim is still current
            # now; a reclaimed/closed task never reaches resume/submit.
            marker = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='owner_admitted' ORDER BY id DESC LIMIT 1",
                                  (params['task_id'], params['run_id'])).fetchone()
            if marker:
                if json.loads(marker[0])['session_id'] != ref.session_id:
                    raise RuntimeStoreError('stale_kanban_claim')
            else:
                task = kb.get_task(conn, params['task_id'])
                if not (task and task.status == 'running' and task.current_run_id == params['run_id'] and task.claim_lock == params['claim_lock']):
                    raise RuntimeStoreError('stale_kanban_claim')
                admitted = {'db': owner_db, 'session_id': ref.session_id,
                            'request_id': request_id, 'pid': os.getpid(), 'birth': worker_birth(psutil.Process())}
                if copied:
                    admitted['resumed_session_id'] = resume_plan['resumed_session_id']
                kb._append_event(conn, task.id, 'owner_admitted', admitted, run_id=task.current_run_id)
                if resume_plan:
                    from gateway.session_kanban_resume import record_resume_event
                    record_resume_event(conn, task.id, task.current_run_id, resume_plan, copied)
    from gateway.session_runtime_workers import tracked_write
    await tracked_write(authority, admit_claim)
    target = request_id
    if saved:  # [daveey] a turn checkpointed at the last owner's restart continues here (session_kanban_continue)
        from functools import partial
        from gateway import session_kanban_continue as cont
        target, checkpoint = await asyncio.to_thread(cont.pending_admission, context['db'], params, request_id)
        if checkpoint is not None:
            await tracked_write(authority, partial(cont.prepare, authority, principal_id=connection.actor.subject,
                session_id=ref.session_id, context=context, checkpoint=checkpoint, request_id=target, owner_db=owner_db))
    await connection.resume(ref, {})
    # Finite like the `chat -q` worker it replaced: nobody can answer an approval or clarify in a
    # card (approvals.single_query_mode applies). Never unattended: that is `-z`'s auto-approval.
    # A retry of an attempt admitted before cards were finite resubmits its committed payload,
    # so the store returns the original receipt instead of admission_conflict.
    from hermes_state_runtime import find_session_admission
    committed = None if not saved else await asyncio.to_thread(find_session_admission, authority.db,
        principal_id=connection.actor.subject, session_id=ref.session_id, request_id=request_id)
    payload: dict = {'text': f'work kanban task {params["task_id"]}'}
    if committed is None or 'finite' in committed['payload']:
        payload['finite'] = True
    # The dispatcher's trusted producer marker (#134889 KB-8): nobody waits on a card attempt, so one
    # lost across an owner restart settles interrupted instead of pausing the card session. An
    # attempt main committed without it resubmits unmarked (same digest, original receipt).
    card = None
    if committed is None or 'kanban_card_v1' in committed['payload']:
        card = {'board': context.get('board'), 'task_id': params['task_id'], 'run_id': params['run_id']}
    if target != request_id:
        from gateway.session_kanban_continue import CONTINUE_TEXT
        payload = {'text': CONTINUE_TEXT, 'finite': True}
        card = {'board': context.get('board'), 'task_id': params['task_id'], 'run_id': params['run_id']}
    receipt = await authority.submit(connection.actor, Submission(target, ref, payload, 'queue'),
                                     _kanban_card=card)
    return {'session_id': ref.session_id, 'receipt': asdict(receipt)}


async def _seed_restart_resume(authority, session_id, policy, candidate, run_id):
    """[daveey] Plan and copy a restart-lost run's transcript into this run's fresh session, off the owner
    loop. ``(None, 0)`` without a candidate; a failure is a recorded fallback (the worker then sees an empty
    history and runs today's transcript-tail context)."""
    if not candidate:
        return None, 0
    from functools import partial
    from types import SimpleNamespace
    from gateway.session_kanban_resume import compaction_threshold_tokens, plan_and_seed
    from gateway.session_runtime_workers import tracked_write
    db = authority.db
    return await tracked_write(authority, partial(
        plan_and_seed, candidate, task=SimpleNamespace(assignee=candidate.get('assignee')), run_id=run_id,
        session_db=db, owner_db=getattr(db, 'db_path', None), session_id=session_id,
        threshold_tokens=lambda: compaction_threshold_tokens(policy)))


def bind_worker_context(frame):
    """Only the adopted private exec calls this, before tool discovery imports."""
    import os
    context = json.loads(frame['policy'].get('kanban_json') or 'null')
    if context is None:
        return
    if frame['policy']['source'] != 'kanban':
        raise ValueError('invalid_kanban_policy')
    env = {'HERMES_KANBAN_TASK': context['task_id'], 'HERMES_KANBAN_BOARD': context['board'],
        'HERMES_KANBAN_DB': context['db'], 'HERMES_KANBAN_RUN_ID': str(context['run_id']),
        'HERMES_KANBAN_CLAIM_LOCK': context['claim_lock'], 'HERMES_KANBAN_WORKSPACE': context['workspace'],
        'HERMES_KANBAN_WORKSPACES_ROOT': context['workspaces_root'], 'HERMES_PROFILE': context['profile'],
        'HERMES_SESSION_SOURCE': 'kanban'}
    for key, value in (('HERMES_KANBAN_BRANCH', context['branch']), ('HERMES_TENANT', context['tenant'])):
        if value:
            env[key] = value
    if context['goal_mode']:
        env['HERMES_KANBAN_GOAL_MODE'] = '1'
    from hermes_cli.kanban_db_dispatch import _worker_terminal_timeout_env
    for key in ('TERMINAL_TIMEOUT', 'TERMINAL_MAX_FOREGROUND_TIMEOUT'):
        value = _worker_terminal_timeout_env(context['max_runtime_seconds'], os.environ.get(key))
        if value:
            env[key] = value
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli import kanban_db as kb
    with connect_closing(Path(context['db'])) as conn:
        with kb.write_txn(conn):
            task = kb.get_task(conn, context['task_id'])
            if (task is None or task.status != 'running' or task.current_run_id != context['run_id']
                    or task.claim_lock != context['claim_lock']):
                raise RuntimeStoreError('stale_kanban_claim')
            # The dispatcher's spawned worker stays the run's pid (reclaim, timeout and operator
            # kills target it; ``_watch_submitter`` ties this interpreter's life to it). The bound
            # interpreter is recorded beside it so a sweep can tell when the attempt is truly gone.
            from hermes_cli.kanban_db_dispatch import UNVERIFIED_WORKER_FINGERPRINT, _process_fingerprint
            fingerprint = _process_fingerprint(os.getpid()) or UNVERIFIED_WORKER_FINGERPRINT
            kb._append_event(conn, task.id, 'worker_bound', {'pid': os.getpid(), 'claim_lock': context['claim_lock'],
                             'started_at': fingerprint}, run_id=context['run_id'])
            claimed = conn.execute("SELECT payload, created_at FROM task_events WHERE task_id=? AND run_id=? AND kind='claimed' ORDER BY id DESC LIMIT 1",
                                   (task.id, context['run_id'])).fetchone()
    # Heartbeats extend the claim by the TTL the dispatcher claimed it with (a spawned worker
    # inherited it from the dispatcher's environment); this daemon's own default is not that.
    expires = json.loads(claimed[0]).get('expires') if claimed else None
    if isinstance(expires, int) and claimed[1]:
        env['HERMES_KANBAN_CLAIM_TTL_SECONDS'] = str(max(1, expires - int(claimed[1])))
    _watch_submitter(Path(context['db']), context['run_id'])
    os.environ.update(env)
    os.chdir(context['workspace'])


_TURNS_DONE = threading.Event()


def _watch_submitter(db, run_id):
    """The dispatcher's spawned worker is this attempt's handle: killing it (an operator, a
    timeout, an OOM kill) must end the attempt, as it did when the worker ran the agent itself.
    Exiting hard leaves no ``worker_result``, so the dead-worker sweep books this run's crash.
    The run row is re-read each tick: the dispatcher records the pid only after its spawn returns."""
    import os
    from hermes_cli.kanban_db_dispatch import _worker_alive

    def watch():
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            while not _TURNS_DONE.wait(1.0):
                try:
                    row = conn.execute('SELECT worker_pid, worker_started_at FROM task_runs WHERE id=?', (run_id,)).fetchone()
                except sqlite3.Error:
                    continue
                if row and row[0] and row[0] != os.getpid() and not _worker_alive(row[0], row[1]):
                    os._exit(1)
    threading.Thread(target=watch, name='kanban-submitter-watch', daemon=True).start()


def run_worker_turns(agent, frame, history):
    from gateway.session_managed_worker import worker_turn_scope
    from gateway.session_surface import arm_surface_turn
    with worker_turn_scope(frame):
        # The agent consumes explicit one-turn attributes, not the ContextVar: arm them exactly as
        # the in-process runner does (voice route marker + surface note on the user message).
        arm_surface_turn(agent)
        return _run_worker_turns(agent, frame, history)


@contextlib.contextmanager
def _worker_workspace(frame):
    """A managed worker's chat turn in its session's workspace, as the in-process turn runner binds it
    (#140665): the session id as the terminal task id, the route as the session key (persistent Docker
    keys the workspace container from it) and the frozen cwd as the session's mount. Yields the task
    id, or None for a frame with no session scope (nothing is bound then)."""
    session_id = (frame.get('scope') or {}).get('session_id')
    if not session_id:
        yield None
        return
    from gateway.session_authorities import served_profile_name
    from gateway.session_context import clear_session_vars, set_session_vars
    from gateway.session_policy import bind_turn_workspace, restore_policy
    from hermes_state_keys import profile_from_session_key
    policy = restore_policy(frame['policy'])
    route = frame.get('route') or session_id
    # The profile its in-process turns bind, so persistent Docker keys the container they and the
    # session's side agents use (#140665): a served secondary's name, which its routes carry
    # (``agent:<name>:``); none for the launch home, whatever its name (``hermes -p beta gateway``
    # keys ``agent:main:``, and only the default profile runs the multiplexer).
    profile = served_profile_name(frame.get('home'))
    tokens = set_session_vars(session_key=route, session_id=session_id,
                              profile=profile if profile_from_session_key(route) == profile else '')
    try:
        bind_turn_workspace(policy, session_id)
        yield session_id
    finally:
        clear_session_vars(tokens)


def _run_worker_turns(agent, frame, history):
    context = json.loads(frame['policy'].get('kanban_json') or 'null')
    if context is None:
        from gateway.session_display import worker_display_kwargs
        author = frame.get('turn_author')
        with _worker_workspace(frame) as task_id:
            return agent.run_conversation(frame['text'], conversation_history=history, **worker_display_kwargs(),
                                          **({'task_id': task_id} if task_id else {}),
                                          **({'turn_author': author} if author is not None else {}))
    from hermes_cli.turn_exit import credential_failure_flags, turn_exit_code
    code, last_output, failure = 1, '', None
    try:
        result = _run_task_turns(agent, frame, history, context)
        failure = result
        code = turn_exit_code(result, kanban_worker=True)
        last_output = str(result.get('final_response') or '')[-500:] if isinstance(result, dict) else ''
        return result
    except Exception as exc:
        failure = exc
        code = turn_exit_code(None, kanban_worker=True, **credential_failure_flags(exc))
        raise
    finally:
        _record_worker_result(context, code, last_output, failure, agent=agent)


def record_start_failure(frame, exc):
    """The worker died before any turn ran (agent construction resolves credentials): book the
    one-shot CLI's credential exit (75 quota wall / 78 re-login / 1) instead of a bare crash.
    An empty, unreadable or invalid host routing registry books the owner-unavailable exit with its
    typed proof (``kanban_host_refusal``): no pin of any card could have started, so the card is not at fault."""
    context = json.loads(frame['policy'].get('kanban_json') or 'null')
    if context is None:
        return
    from hermes_cli.turn_exit import credential_failure_flags, turn_exit_code
    from hermes_cli.kanban_host_refusal import PROOF_KEY, startup_proof
    from hermes_cli.kanban_quota_probe import contradicted
    booked = contradicted(exc)  # [daveey] a typed wall the live usage read disputes books no reset
    proof = startup_proof(booked)
    if proof is None:
        code = turn_exit_code(None, kanban_worker=True, **credential_failure_flags(booked))
    else:
        from hermes_cli.kanban_owner_unavailable import KANBAN_OWNER_UNAVAILABLE_EXIT_CODE as code
    _record_worker_result(context, code, start_failure_line(booked), booked,
                          extra={PROOF_KEY: proof} if proof else None)
    exc._kanban_failure_booked = True


def record_worker_failure(frame, exc):
    """[daveey] Any other exception that kills a managed Kanban worker (a turn, the close-time token
    flush, store.finish): book its cause as the attempt's last word, after whatever the turn booked.
    Run 10210 died in ``flush_token_counts`` (WorkerPersistenceError) after a class failover and its
    card said only "Kanban execution unknown". Never raises: the worker's own exit must not depend on it."""
    if getattr(exc, '_kanban_failure_booked', False):
        return
    try:
        context = json.loads(((frame or {}).get('policy') or {}).get('kanban_json') or 'null')
        if context is None:
            return
        from hermes_cli.turn_exit import credential_failure_flags, turn_exit_code
        _record_worker_result(context, turn_exit_code(None, kanban_worker=True, **credential_failure_flags(exc)),
                              failure_line(exc, 'Worker failed'), exc)
    except Exception:
        import logging
        logging.getLogger(__name__).debug('could not book the worker failure cause', exc_info=True)


def failure_line(exc, prefix):
    from agent.redact import redact_sensitive_text
    text = ' '.join(f'{type(exc).__name__}: {exc}'.split())
    return f'{prefix}: ' + redact_sensitive_text(text, force=True)[:300]


def start_failure_line(exc):
    """[daveey] One redacted line naming why the worker could not start. Without it the card's crash
    record read only "Kanban execution unknown; retained by owner" and the cause (e.g. a
    ModelRoutingError) lived only in managed-worker.log, so every crashed card had to be diagnosed by hand."""
    return failure_line(exc, 'Worker could not start')


def _record_worker_result(context, code, last_output, failure=None, extra=None, *, agent=None):
    _TURNS_DONE.set()
    import os
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_quota_receipt import quota_fields, route_fields
    observer_error = None
    strict_death_proof = False
    if not (extra or {}).get('continuable'):
        from hermes_cli.kanban_failed_result import modern_result_recorded, recorded_process_witnesses
        with connect_closing(Path(context['db'])) as conn:
            if not _bound_worker_matches(conn, context):
                return
            # [daveey] A clean exit joins the modern evidence only on a run that already holds it (a turn
            # continued after its owner's restart booked a modern exit 76). Unmarked, it is never
            # attributable there, so the crash sweep held the run forever (t_01a1d563324d7e8e run 11569).
            strict_death_proof = recorded_process_witnesses(conn, context) and bool(
                code or modern_result_recorded(conn, context['task_id'], context['run_id']))
        if strict_death_proof and code:
            from hermes_cli.kanban_failed_result_observer import arm
            try:
                arm(context)
            except (OSError, RuntimeError) as exc:
                observer_error = str(exc)[:300]
    with connect_closing(Path(context['db'])) as conn, kb.write_txn(conn):
        if _bound_worker_matches(conn, context):
            if observer_error:
                kb._append_event(conn, context['task_id'], 'worker_result_observer_failed',
                                 {'error': observer_error}, run_id=context['run_id'])
            from hermes_cli.kanban_failed_result_identity import result_identity_fields
            identity_fields = result_identity_fields(conn, context) if strict_death_proof else {}
            # Closing a run clears its claim/PID and replaces metadata; keep the
            # result in the immutable attempt event stream instead.
            kb._append_event(conn, context['task_id'], 'worker_result',
                {'pid': os.getpid(), 'claim_lock': context['claim_lock'], 'exit_code': code,
                 'last_output': last_output, **quota_fields(failure), **route_fields(failure, agent),
                 **(extra or {}), **identity_fields,
                 **({'death_proof_version': 1} if strict_death_proof else {})},
                run_id=context['run_id'])


def record_worker_loss(policy, reason, *, pid=None, bound=False):
    """Append a run-scoped ``worker_lost`` event for this attempt to the card's board.

    Dropped when the card has moved past the run (it says nothing about the current attempt).
    Never raises: the caller's own loss handling must not depend on the board write.
    """
    try:
        context = json.loads(policy.kanban_json or 'null')
        if not context:
            return
        from hermes_cli.kanban_db_connect import connect_closing
        from hermes_cli import kanban_db as kb
        run_id = int(context['run_id'])
        with connect_closing(Path(context['db'])) as conn, kb.write_txn(conn):
            row = conn.execute('SELECT status, current_run_id FROM tasks WHERE id=?',
                               (context['task_id'],)).fetchone()
            if row is None or row[0] != 'running' or row[1] != run_id:
                return
            from hermes_cli.kanban_lost_result import loss_identity_fields
            witness = loss_identity_fields(conn, context, pid, bound)
            kb._append_event(conn, context['task_id'], 'worker_lost',
                             {'reason': str(reason)[:200], 'claim_lock': context['claim_lock'],
                              'run_id': run_id, 'pid': pid, 'bound': bool(bound), **witness},
                             run_id=run_id)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('kanban worker_lost record failed')


def _bound_worker_matches(conn, context):
    import os
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_bound' ORDER BY id DESC LIMIT 1",
        (context['task_id'], context['run_id'])).fetchone()
    bound = json.loads(row[0]) if row else {}
    return (bound.get('pid'), bound.get('claim_lock')) == (os.getpid(), context['claim_lock'])


def _bound_claim_is_live(context):
    """Retry proof for a managed worker: the frame's exact run/claim lease is live and unexpired,
    and THIS interpreter is the one ``bind_worker_context`` bound to it. The run's worker_pid is
    the dispatcher's spawned submitter here, so pid identity comes from ``worker_bound`` instead."""
    from agent.kanban_turn_recovery import CLAIM_LIVE
    return _bound_claim_state(context) == CLAIM_LIVE


def _bound_claim_state(context):
    """``worker_claim_state`` for the frame's exact carrier (``live``/``lapsed``/``lost``), and
    ``lost`` unless THIS interpreter is the one ``worker_bound`` recorded under that claim. A
    ``lapsed`` lease is still ours in every coordinate; recovery waits for the dispatcher to renew it."""
    from agent.kanban_turn_recovery import CLAIM_LOST, worker_claim_state
    state = worker_claim_state(task_id=context['task_id'], db_path=context['db'], run_id=str(context['run_id']),
                               claim_lock=context['claim_lock'], verify_pid=False)
    if state == CLAIM_LOST:
        return CLAIM_LOST
    try:
        with closing(sqlite3.connect(Path(context['db']).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            return state if _bound_worker_matches(conn, context) else CLAIM_LOST
    except sqlite3.Error:
        return CLAIM_LOST


def worker_result(path, params):
    """Read only the dispatcher's exact attempt; never infer success from admission settlement."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        row = conn.execute("SELECT payload FROM task_events WHERE run_id=? AND task_id=? AND kind='worker_result' ORDER BY id DESC LIMIT 1",
            (params['run_id'], params['task_id'])).fetchone()
        result = json.loads(row[0]) if row else {}
        return result if result.get('claim_lock') == params['claim_lock'] else {}


def worker_exit_code(path, params):
    return worker_result(path, params).get('exit_code', 1)


# Who wrote a card attempt's prompts: the dispatcher, never the operator, so memory providers do not
# file "work kanban task t_..." and the worker's progress notes as the human's own turns (#137406).
# Not a ``bot:<profile>`` id: a Bot Mode profile named ``kanban`` would share its memory key.
KANBAN_TURN_AUTHOR = {'id': 'kanban:dispatcher', 'name': 'Kanban dispatcher', 'is_bot': True}


def _run_task_turns(agent, frame, history, context):
    from gateway.session_kanban_continue import is_continuation
    if is_continuation(frame.get('text')) and history:
        # [daveey] The turn checkpointed at the last owner's restart continues: its skills and card
        # context are already in the session's history, so only the continuation note is new.
        prompt = frame['text']
    else:
        prompt, history = _first_turn_prompt(agent, frame, history, context)
    result = agent.run_conversation(prompt, conversation_history=history, turn_author=KANBAN_TURN_AUTHOR)

    def recover(nudge):
        nonlocal result
        prior = result.get('messages') if isinstance(result, dict) else None
        result = agent.run_conversation(nudge, conversation_history=prior or
                                        agent._session_db.get_messages_as_conversation(agent.session_id),
                                        turn_author=KANBAN_TURN_AUTHOR)
    # Same in-place retry as the one-shot CLI worker (agent/kanban_turn_recovery.py), but the
    # carrier is this frame's bound claim, never the owner process's environment.
    from agent.kanban_turn_recovery import recover_failed_kanban_turns
    recover_failed_kanban_turns(recover, lambda: result, task_id=context['task_id'],
                                claim_state=lambda: _bound_claim_state(context))
    from hermes_cli.turn_exit import turn_exit_code
    # Only a settled turn may continue the goal loop: an exhausted or denied recovery reaches
    # the honest exit path without re-entering the model under an unprovable claim.
    if not context['goal_mode'] or turn_exit_code(result, kanban_worker=True) != 0:
        return result
    from hermes_cli.goals import run_kanban_goal_loop, DEFAULT_MAX_TURNS
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        judged_task = kb.get_task(conn, context['task_id'])
    def status():
        with connect_closing() as conn:
            return kb.goal_run_status(conn, context['task_id'], context['run_id'])
    def block(reason):
        with connect_closing() as conn:
            kb.block_task(conn, context['task_id'], reason=reason, expected_run_id=context['run_id'])
    def unachievable(reason):
        from hermes_cli.kanban_goal_recovery import park
        with connect_closing() as conn:
            return park(conn, judged_task, reason, expected_run_id=context['run_id'])
    def turn(prompt):
        nonlocal result
        result = agent.run_conversation(prompt, conversation_history=agent._session_db.get_messages_as_conversation(agent.session_id),
                                        turn_author=KANBAN_TURN_AUTHOR)
        return result.get('final_response', '')
    from hermes_cli.goals import kanban_goal_loop_turn_result
    loop = run_kanban_goal_loop(task_id=context['task_id'], goal_text=context['goal_text'], run_turn=turn,
        task_status_fn=status, block_fn=block, max_turns=context['goal_max_turns'] or DEFAULT_MAX_TURNS,
        first_response=result.get('final_response', ''), unachievable_fn=unachievable)
    # A broken loop exits non-zero; a parked or dispatcher-owned stop keeps the settled result (rc 0).
    return kanban_goal_loop_turn_result(result, loop)


def _first_turn_prompt(agent, frame, history, context):
    """``(prompt, history)`` of an attempt's first turn: pinned skills, the card context and the frame text."""
    from agent.skill_commands import build_preloaded_skills_prompt
    skills, _loaded, missing = build_preloaded_skills_prompt(context['skills'], task_id=agent.session_id)
    # Pins were validated when the card was created; a later removal must reach the worker,
    # not crash it before it can report or recover the stale prerequisite.
    from hermes_cli.kanban_skill_check import unavailable_pin_note
    warning = unavailable_pin_note(missing)
    if warning:
        import logging
        logging.getLogger(__name__).warning('%s', warning)
    from gateway.session_kanban_resume import compact_resumed_history
    from gateway.session_kanban_capsule import capsule_prompt_context
    # [daveey] An over-headroom resumed transcript is compacted before the first model call (or dropped).
    history = compact_resumed_history(agent, context, history)
    card = capsule_prompt_context(context, history)  # [daveey] resumed after an owner restart, or the tail
    return '\n\n'.join(p for p in (skills, warning, card, frame['text']) if p), history
