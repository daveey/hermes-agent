"""An uncertainty hold belongs to all unresolved fires, not just its creator."""
import asyncio
import json
import threading

import pytest

from cron import executions, jobs, scheduler_authority


def _fire(job):
    fire = executions.create_execution(job['id'], source='test')['id']
    executions.mark_execution_running(fire)
    journal = scheduler_authority.journal_path(job['id'], fire)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({'params': {'job_id': job['id'], 'request_id': fire,
                                             'extra_prompt': None}}))
    return fire, journal


@pytest.fixture
def owner(monkeypatch):
    from gateway import session_cron

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    states = {}
    monkeypatch.setattr(session_cron, 'owner_for_home', lambda home: (object(), loop))

    async def operation(authority, op, params):
        assert op == 'recover'
        return states[params['request_id']]

    monkeypatch.setattr(session_cron, 'operation', operation)
    try:
        yield states
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        assert not thread.is_alive()
        loop.close()


@pytest.mark.parametrize('creator_finishes_first', [True, False])
def test_shared_hold_survives_until_both_receipts_terminal(tmp_path, owner, creator_finishes_first):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        sibling, sibling_journal = _fire(job)
        executions.give_up_execution(sibling, reason='firer crashed')
        creator, creator_journal = _fire(job)
        scheduler_authority.pause_for_unknown(job['id'], creator, 'Cron execution unverified')
        executions.give_up_execution(creator, reason='observer lost')
        owner[sibling] = {'status': 'unknown'}
        owner[creator] = {'status': 'unknown'}
        scheduler_authority.reconcile_pending()
        first, last = (creator, sibling) if creator_finishes_first else (sibling, creator)
        owner[first] = {'status': 'terminal', 'result': [True, 'output', 'answer', None],
                        'job': jobs.get_job(job['id'])}
        scheduler_authority.reconcile_pending()
        assert not jobs.get_job(job['id'])['enabled']
        assert scheduler_authority.journal_path(job['id'], last).exists()
        assert not scheduler_authority.journal_path(job['id'], first).exists()
        owner[last] = {'status': 'terminal', 'result': [True, 'output', 'answer', None],
                       'job': jobs.get_job(job['id'])}
        scheduler_authority.reconcile_pending()
        assert jobs.get_job(job['id'])['enabled']
        assert not sibling_journal.exists()
        assert not creator_journal.exists()


def test_disabled_job_is_not_reenabled_by_terminal_retirement(tmp_path):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        jobs.update_job(job['id'], {'enabled': False})
        fire, journal = _fire(job)
        scheduler_authority.pause_for_unknown(job['id'], fire, 'Cron execution unverified')
        executions.finish_execution(fire, success=False, error='stopped')
        scheduler_authority.retire_terminal_journal(job['id'], fire)
        assert not jobs.get_job(job['id'])['enabled']
        assert not journal.exists()
