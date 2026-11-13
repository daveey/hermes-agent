"""Cron admissions retain exact job identity and stored-job model policy."""
import asyncio
from types import SimpleNamespace

import pytest


def test_cron_admission_uses_job_model_and_exact_retry(tmp_path, monkeypatch):
    from cron import jobs
    from gateway import run, session_cron
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from gateway.session_contract import SessionActor
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch, RuntimeStoreError

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(jobs, 'get_job', lambda jid: {'id': jid, 'prompt': 'original', 'model': 'per-job-model'})
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'model': {}, 'platform_toolsets': {'cli': []}})
    db = SessionDB(tmp_path / 'state.db')
    from gateway.config import GatewayConfig
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = SimpleNamespace(_draining=False, session_store=store, adapters={})
    authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='test', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='test'))
    runner.session_authority = authority
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    actor = SessionActor('owner', str(tmp_path), frozenset({'session:create', 'session:submit', 'session:control'}), 'viewer')

    async def probe():
        params = {'job_id': 'job', 'request_id': 'fire', 'extra_prompt': 'extra'}
        receipt = await session_cron.operation(authority, 'submit', params, actor)
        assert await session_cron.operation(authority, 'submit', params, actor) == receipt
        assert await session_cron.operation(authority, 'submit', params) == receipt
        with pytest.raises(RuntimeStoreError):
            await session_cron.operation(authority, 'submit', dict(params, extra_prompt='changed'), actor)
        policy = runner.adapters[next(iter(runner.adapters))].policies[receipt['session_id']]
        assert policy.model == 'per-job-model'
        assert policy.source == 'cron'

    try:
        asyncio.run(probe())
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cron_cancel_between_claim_and_execution_registration_stops_the_job(tmp_path, monkeypatch):
    """A started admission whose execute() has not registered its event yet still honours cancel."""
    from contextlib import nullcontext
    import json
    from cron import scheduler
    from gateway import run, session_cron
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionActor, SessionRef
    from hermes_state import SessionDB
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('s', source='cron')
    authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id='p', instance_id='test', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='test'))
    authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='cron-owner'), 'route')
    actor = SessionActor('cron-owner', 'p', frozenset({'session:create', 'session:submit', 'session:control'}), 'ticker')
    ref = SessionRef('p', 's')
    monkeypatch.setattr(run, '_profile_runtime_scope', lambda home: nullcontext())
    seen = {}

    def run_job(job, *, extra_prompt, execution_id, cancel_event):
        seen['cancelled_at_start'] = cancel_event.is_set()
        return (False, '', '', 'cancelled')
    monkeypatch.setattr(scheduler, 'run_job', run_job)

    with db:
        admit_session_input(db, epoch=authority.epoch, principal_id='cron-owner', session_id='s',
                            request_id='cron:job:fire', payload={'text': ''})
        row = claim_session_input(db, epoch=authority.epoch, session_id='s')
        # The claim is committed and visible as `started`; execute() has not run yet.
        params = {'session_id': 's', 'admission_id': row['admission_id']}
        assert await session_cron.operation(authority, 'cancel', params, actor) == {'ok': True}
        policy = SimpleNamespace(request_json=json.dumps({
            'extra_prompt': None, 'request_id': 'cron:job:fire', 'cron_job': {'id': 'job'}}))
        with pytest.raises(RuntimeError):
            await session_cron.execute(authority, ref, row, policy)
    assert seen == {'cancelled_at_start': True}
    assert authority._cron_cancellations == {}


@pytest.mark.asyncio
async def test_owner_execution_binds_the_scheduler_cron_identity_inside_run_job(tmp_path, monkeypatch):
    """The owner runs the job on its own loop thread, which never saw the firing scheduler's
    ``enter_cron_execution``: ``ctx.current_cron_execution()`` must still name this fire, keyed on
    the scheduler's execution id (the admission's request id), never on the admission id."""
    from contextlib import nullcontext
    import json
    from cron import scheduler
    from cron.execution_identity import current_cron_execution
    from cron.executions import create_execution, mark_execution_running
    from gateway import run, session_cron
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionRef
    from hermes_state import SessionDB
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    execution = create_execution('job', source='builtin', scheduled_instant='2026-10-08T09:00:00')
    record = mark_execution_running(execution['id'])
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('s', source='cron')
    authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id='p', instance_id='test', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='test'))
    authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='cron-owner'), 'route')
    monkeypatch.setattr(run, '_profile_runtime_scope', lambda home: nullcontext())
    seen = {}

    def run_job(job, *, extra_prompt, execution_id, cancel_event):
        seen['execution_id'] = execution_id
        seen['identity'] = current_cron_execution()
        return (True, 'doc', 'answer', None)
    monkeypatch.setattr(scheduler, 'run_job', run_job)
    request_id = 'cron:job:' + execution['id']
    with db:
        admit_session_input(db, epoch=authority.epoch, principal_id='cron-owner', session_id='s',
                            request_id=request_id, payload={'text': ''})
        row = claim_session_input(db, epoch=authority.epoch, session_id='s')
        policy = SimpleNamespace(request_json=json.dumps({
            'extra_prompt': None, 'request_id': request_id, 'cron_job': {'id': 'job', 'name': 'Job'}}))
        assert await session_cron.execute(authority, SessionRef('p', 's'), row, policy) == 'answer'
    ident = seen['identity']
    assert seen['execution_id'] == execution['id'] != row['admission_id']
    assert ident is not None and (ident.job_id, ident.job_name, ident.execution_id) == ('job', 'Job', execution['id'])
    assert (ident.source, ident.scheduled_instant, ident.started_at) == (
        'builtin', record['scheduled_instant'], record['started_at'])
    assert current_cron_execution() is None


def test_draining_owner_still_answers_status_and_cancel_of_admitted_cron_work(tmp_path, monkeypatch):
    """A stop/restart sets ``_draining`` while it waits for in-flight cron work. The in-gateway
    firer polls ``status`` until the run settles: refusing it made the firer raise
    CronExecutionUnknown and pause the job although the run completed. New fires stay fenced."""
    import threading
    from cron import jobs
    from cron.scheduler_authority import run_canonical_job
    from gateway import run, session_cron
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from hermes_state import SessionDB
    from hermes_state_runtime import (RuntimeStoreError, begin_runtime_epoch, claim_session_input,
                                      settle_session_input)

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(jobs, 'get_job', lambda jid: {'id': jid, 'prompt': 'p', 'model': 'm'})
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'model': {}, 'platform_toolsets': {'cli': []}})
    db = SessionDB(tmp_path / 'state.db')
    runner = SimpleNamespace(_draining=False, session_store=SessionStore(tmp_path / 'sessions', GatewayConfig()),
                             adapters={})
    authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='test', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='test'))
    runner.session_authority = authority
    admitted = threading.Event()
    monkeypatch.setattr(authority, '_schedule', lambda ref: admitted.set())
    polled_while_draining = threading.Event()
    real_operation = session_cron.operation

    async def observed_operation(owner, name, params, actor=None):
        draining = runner._draining
        result = await real_operation(owner, name, params, actor)
        if name == 'status' and draining:
            polled_while_draining.set()  # answered, not refused, during the drain
        return result
    monkeypatch.setattr(session_cron, 'operation', observed_operation)

    async def probe():
        session_cron.bind_owner(authority)
        try:
            firer = asyncio.create_task(asyncio.to_thread(run_canonical_job, {'id': 'job'}, execution_id='fire'))
            assert await asyncio.to_thread(admitted.wait, 10), firer
            runner._draining = True  # shutdown begins while the admitted run executes
            sid = next(iter(authority.sessions))
            row = claim_session_input(db, epoch=authority.epoch, session_id=sid)
            assert await session_cron.operation(
                authority, 'cancel', {'session_id': sid, 'admission_id': row['admission_id']}) == {'ok': True}
            # the firer polls status at least once during the drain, and is answered
            assert await asyncio.to_thread(polled_while_draining.wait, 10), 'firer never polled status during the drain'
            settle_session_input(db, epoch=authority.epoch, admission_id=row['admission_id'],
                                 generation=row['generation'], outcome='completed',
                                 result={'result': {'cron_result': [True, 'doc', 'answer', None]}, 'usage': {}})
            assert await asyncio.wait_for(firer, 10) == (True, 'doc', 'answer', None)
            with pytest.raises(RuntimeStoreError, match='runtime_draining'):
                await session_cron.operation(authority, 'submit',
                                             {'job_id': 'job', 'request_id': 'next', 'extra_prompt': None})
        finally:
            session_cron.unbind_owner(authority)

    try:
        asyncio.run(probe())
    finally:
        db.close()


@pytest.mark.asyncio
async def test_draining_owner_answers_an_external_firers_cron_observation_over_the_socket(tmp_path, monkeypatch):
    """The restart-safe cron worker fires out of process: it submits and then polls ``cron.status``
    over an operator WebSocket. A restart's after-turn wait refused every operator verb outside the
    interactive allowlist with ``runtime_draining``, so the worker's poll was refused, the firer gave
    up (CronExecutionUnknown: pause + released fire claim) and the still-running gateway-side run
    then died with "cron fire ownership was lost" (hub kanban-hub-pin-sweep, 2026-11-12 12:47Z).
    Observing/stopping admitted work stays reachable during the drain; a new submit stays fenced."""
    from cron import jobs
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(jobs, 'get_job', lambda jid: {'id': jid, 'prompt': 'p', 'model': 'm'})
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'model': {}, 'platform_toolsets': {'cli': []}})
    db = SessionDB(tmp_path / 'state.db')
    runner = SimpleNamespace(_draining=False, _running=True,
                             session_store=SessionStore(tmp_path / 'sessions', GatewayConfig()), adapters={})
    authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='owner', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='owner'))
    runner.session_authority = authority
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    firer = AuthorityConnection(authority, object(), {'user_id': 'owner'}, operator=True)
    fire = {'job_id': 'job', 'request_id': 'fire', 'extra_prompt': None}

    async def call(method, **params):
        return await firer.dispatch({'id': 1, 'method': method, 'params': params})

    try:
        submitted = await call('cron.submit', **fire)
        receipt = submitted['result']
        runner._draining = True  # a restart's after-turn wait begins while the fire runs
        runner.session_runtime_descriptor = {'state': 'draining', 'drain_reason': 'restart_wait'}
        status = await call('cron.status', **receipt)
        assert 'error' not in status, status
        assert status['result']['status'] == 'queued'
        recovered = await call('cron.recover', **fire)
        assert 'error' not in recovered, recovered
        assert recovered['result']['admission_id'] == receipt['admission_id']
        cancelled = await call('cron.cancel', **receipt)
        assert cancelled.get('error', {}).get('message') != 'runtime_draining', cancelled
        refused = await call('cron.submit', **dict(fire, request_id='next'))
        assert refused['error']['message'] == 'runtime_draining'
    finally:
        await firer.close()
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stop_case', ['shutdown', 'aborted_drain', 'interrupt_only'])
async def test_forwarded_cancel_during_owner_restart_stays_a_restart_cut_after_the_drain_ends(
        tmp_path, monkeypatch, caplog, stop_case):
    """cron-7c04faf754 round 2: the fire was handed to a restart-safe worker, so this owner's
    process holds no scheduler interrupted mark for it. The stop forwarded while the owner drains
    for its restart must keep meaning "this owner's shutdown" after ``_stop_persist_exit_state``
    resets ``_draining`` and before the run thread reads its latch: one WARNING ``stopped by gateway
    shutdown``, no ``failed:`` ERROR, and the restart-cut flag the firer books as an interruption."""
    from contextlib import nullcontext
    import json
    import logging
    from cron import scheduler
    from cron.cancellation import CronShutdownInterrupted, cancel_is_shutdown, cancel_reason
    from cron.restart_cut import RESTART_CUT_FLAG
    from gateway import run, session_cron
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionActor, SessionRef
    from hermes_state import SessionDB
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('s', source='cron')
    runner = SimpleNamespace(_draining=False, _interrupting_remaining_work=False)
    authority = SessionAuthority(runner, profile_id='p', instance_id='test', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='test'))
    authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='cron-owner'), 'route')
    actor = SessionActor('cron-owner', 'p', frozenset({'session:create', 'session:submit', 'session:control'}), 'ticker')
    monkeypatch.setattr(run, '_profile_runtime_scope', lambda home: nullcontext())
    loop = asyncio.get_running_loop()
    seen = {}

    def run_job(job, *, extra_prompt, execution_id, cancel_event):
        if stop_case == 'interrupt_only':
            # Legacy/raw cancellation has no owner_shutdown latch. Only the sticky interrupt
            # remains after stop(), so execute's fallback must share _owner_draining's meaning.
            cancel = authority._cron_cancellations[row['admission_id']]
            cancel.set()
            assert not getattr(cancel, 'owner_shutdown', False)
            runner._interrupting_remaining_work = True
        else:
            runner._draining = True  # the owner's restart-wait drain begins mid-run
            runner._running = True
            asyncio.run_coroutine_threadsafe(session_cron.operation(authority, 'cancel', params, actor), loop).result(10)
            runner._draining = False
            if stop_case == 'aborted_drain':
                # The forwarded cancel can be a real lost claim: aborting restart-wait keeps
                # serving, but deliberately does not revoke this run's safe-side rerun latch.
                assert runner._running and not runner._interrupting_remaining_work
                assert authority._cron_cancellations[row['admission_id']].owner_shutdown
            else:
                runner._running = False  # _stop_persist_exit_state before the next poll
        seen['reason'] = reason = cancel_reason(cancel_event)
        seen['shutdown'] = cancel_is_shutdown(cancel_event)
        error = CronShutdownInterrupted if seen['shutdown'] else RuntimeError
        exc = error(f"Cron job 'job' was stopped: {reason}")
        if not scheduler._log_expected_stop('job', str(exc), exc):
            scheduler.logger.error("Job '%s' failed: %s", 'job', str(exc))
        return (False, 'doc', '', str(exc))
    monkeypatch.setattr(scheduler, 'run_job', run_job)
    caplog.set_level(logging.DEBUG, logger='cron.scheduler')

    with db:
        admit_session_input(db, epoch=authority.epoch, principal_id='cron-owner', session_id='s',
                            request_id='cron:job:fire', payload={'text': ''})
        row = claim_session_input(db, epoch=authority.epoch, session_id='s')
        params = {'session_id': 's', 'admission_id': row['admission_id']}
        policy = SimpleNamespace(request_json=json.dumps({
            'extra_prompt': None, 'request_id': 'cron:job:fire', 'cron_job': {'id': 'job'}}))
        with pytest.raises(RuntimeError):
            await session_cron.execute(authority, SessionRef('p', 's'), row, policy)
    assert seen['shutdown'] is True, seen['reason']
    records = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == 'cron.scheduler']
    assert [lvl for lvl, _ in records] == [logging.WARNING], records
    assert 'stopped by gateway shutdown' in records[0][1]
    assert authority.pending_results[row['admission_id']]['result']['cron_job_flags'].get(RESTART_CUT_FLAG) is True


@pytest.mark.asyncio
async def test_restart_cut_reports_the_attempts_awake_and_wall_clock(tmp_path, monkeypatch):
    """[daveey] cron-7dfb141525: the owner reports how long the cut attempt was awake (monotonic,
    which stops while the host sleeps) and how long it took on the wall clock, so the firer can
    re-arm a rerun the host slept through (cron/restart_rerun.py ``slept_through``)."""
    from contextlib import nullcontext
    import json
    from cron import scheduler
    from cron.restart_cut import AWAKE_FLAG, RESTART_CUT_FLAG
    from gateway import run, session_cron
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionRef
    from hermes_state import SessionDB
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('s', source='cron')
    runner = SimpleNamespace(_draining=False, _interrupting_remaining_work=False)
    authority = SessionAuthority(runner, profile_id='p', instance_id='test', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='test'))
    authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='cron-owner'), 'route')
    monkeypatch.setattr(run, '_profile_runtime_scope', lambda home: nullcontext())
    # The owner's clocks only; asyncio keeps the real ones. Each read returns the current value,
    # so the fake never runs out however often it is read.
    clocks = {'awake': 1000.0, 'wall': 1_790_000_000.0}
    monkeypatch.setattr(session_cron, 'time', SimpleNamespace(
        monotonic=lambda: clocks['awake'], time=lambda: clocks['wall']))

    def run_job(job, *, extra_prompt, execution_id, cancel_event):
        clocks['awake'] += 50  # awake for 50 s of a 15-minute attempt: the host slept the rest
        clocks['wall'] += 900
        runner._interrupting_remaining_work = True  # this owner's restart stops the run
        return (False, '', '', 'interrupted')
    monkeypatch.setattr(scheduler, 'run_job', run_job)

    with db:
        admit_session_input(db, epoch=authority.epoch, principal_id='cron-owner', session_id='s',
                            request_id='cron:job:fire', payload={'text': ''})
        row = claim_session_input(db, epoch=authority.epoch, session_id='s')
        policy = SimpleNamespace(request_json=json.dumps({
            'extra_prompt': None, 'request_id': 'cron:job:fire', 'cron_job': {'id': 'job'}}))
        with pytest.raises(RuntimeError):
            await session_cron.execute(authority, SessionRef('p', 's'), row, policy)
    flags = authority.pending_results[row['admission_id']]['result']['cron_job_flags']
    assert flags.get(RESTART_CUT_FLAG) is True
    assert flags.get(AWAKE_FLAG) == {'awake': 50.0, 'wall': 900.0}



@pytest.mark.asyncio
@pytest.mark.parametrize('error_name', ['CronRunCancelled', 'CronShutdownInterrupted', 'RuntimeError'])
async def test_requested_cron_stop_is_not_an_admitted_turn_failure(tmp_path, monkeypatch, caplog, error_name):
    """A deliberate stop (remove_job / ``hermes cron cancel``: CronRunCancelled; this gateway's own
    shutdown: CronShutdownInterrupted) already logs one ``cron.scheduler`` WARNING. The admitted turn
    that carried it must not add ``ERROR gateway.session_authority: Admitted turn … failed`` with a
    traceback; a real failure (lost ownership: a plain RuntimeError) keeps it. Composed through
    ``_execute_claimed`` and the real ``session_cron.execute``; the turn still settles ``failed``."""
    from contextlib import nullcontext
    import json
    import logging
    from cron import cancellation, scheduler
    from gateway import run, session_cron, session_finite
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionRef
    from hermes_state import SessionDB
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('s', source='cron')
    authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id='p', instance_id='test', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='test'))
    live = authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='cron-owner'), 'route')
    monkeypatch.setattr(run, '_profile_runtime_scope', lambda home: nullcontext())
    error = getattr(cancellation, error_name, RuntimeError)

    def run_job(job, *, extra_prompt, execution_id, cancel_event):
        # run_job's own except-branch: the scheduler's message and log for the raised stop.
        exc = error("Cron job 'probe' was stopped: job removed while running")
        message = scheduler._job_error_message(exc)
        if not scheduler._log_expected_stop('probe', message, exc):
            scheduler.logger.error("Job '%s' failed: %s", 'probe', message)
        return (False, 'doc', '', message)
    monkeypatch.setattr(scheduler, 'run_job', run_job)
    policy = SimpleNamespace(request_json=json.dumps({
        'extra_prompt': None, 'request_id': 'cron:job:fire', 'cron_job': {'id': 'job', 'name': 'probe'}}))

    async def execute_finite_admission(authority, ref, row):
        return await session_cron.execute(authority, ref, row, policy)
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute_finite_admission)
    caplog.set_level(logging.DEBUG, logger='gateway.session_authority')

    with db:
        admit_session_input(db, epoch=authority.epoch, principal_id='cron-owner', session_id='s',
                            request_id='cron:job:fire', payload={'text': ''})
        row = claim_session_input(db, epoch=authority.epoch, session_id='s')
        response, outcome = await authority._execute_claimed(SessionRef('p', 's'), live, row)
    assert (response, outcome) == ('The admitted turn failed.', 'failed')
    assert authority.pending_results[row['admission_id']]['result']['failed'] is True
    records = [r for r in caplog.records if r.name == 'gateway.session_authority']
    if error is RuntimeError:
        assert [(r.levelno, bool(r.exc_info)) for r in records] == [(logging.ERROR, True)], records
        return
    assert len(records) <= 1, records
    assert all(r.levelno <= logging.WARNING and not r.exc_info for r in records), records
    assert all('failed' not in r.getMessage() for r in records), records
