"""A firer-booked terminal outcome must retire its uncertainty pause too."""
import json

import pytest

from cron import executions, jobs, scheduler, scheduler_authority
from cron.scheduler_bookkeeping import finish_completed_run


def _paused_fire(job):
    fire = executions.create_execution(job['id'], source='test')['id']
    executions.mark_execution_running(fire)
    journal = scheduler_authority.journal_path(job['id'], fire)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({'params': {'job_id': job['id'], 'request_id': fire,
                                             'extra_prompt': None}, 'receipt': None}))
    scheduler_authority.pause_for_unknown(job['id'], fire, 'Cron execution unverified: observer lost')
    return fire, journal


@pytest.mark.parametrize('cancelled', [False, True])
@pytest.mark.parametrize('operator_hold', [False, True])
def test_firer_finished_recovery_retires_only_its_pause(tmp_path, monkeypatch, operator_hold, cancelled):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire, journal = _paused_fire(job)
        executions.finish_execution(fire, success=False, error='stopped', cancelled=cancelled)
        before = executions.get_execution(fire)
        assert before['status'] == ('cancelled' if cancelled else 'failed')
        if operator_hold:
            jobs.pause_job(job['id'], reason='operator hold')
        monkeypatch.setattr(scheduler, '_save_compose_deliver',
                            lambda *a, **kw: pytest.fail('terminal result must not be replayed'))
        scheduler_authority.reconcile_pending(allow_connect=False)
        saved = jobs.get_job(job['id'])
        assert saved['enabled'] is (not operator_hold)
        assert saved['paused_reason'] == ('operator hold' if operator_hold else None)
        assert executions.get_execution(fire) == before
        assert not journal.exists()


@pytest.mark.parametrize('operator_hold', [False, True])
def test_ordinary_tail_retires_only_its_pause(tmp_path, operator_hold):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire, journal = _paused_fire(job)
        if operator_hold:
            jobs.pause_job(job['id'], reason='operator hold')
        delivery = scheduler._RunDelivery(dict(job, execution_id=fire), False, 'stopped')
        assert finish_completed_run(delivery, None, fire)
        saved = jobs.get_job(job['id'])
        assert saved['enabled'] is (not operator_hold)
        assert saved['paused_reason'] == ('operator hold' if operator_hold else None)
        assert executions.get_execution(fire)['status'] == 'failed'
        assert not journal.exists()


def test_failed_lift_keeps_terminal_verdict_and_retries(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire, journal = _paused_fire(job)
        original_resume = jobs.resume_job

        def unavailable(*args):
            raise OSError('store temporarily unavailable')

        monkeypatch.setattr(jobs, 'resume_job', unavailable)
        delivery = scheduler._RunDelivery(dict(job, execution_id=fire), True, None)
        assert finish_completed_run(delivery, None, fire)
        assert executions.get_execution(fire)['status'] == 'completed'
        assert jobs.get_job(job['id'])['last_status'] == 'ok'
        assert journal.exists()
        monkeypatch.setattr(jobs, 'resume_job', original_resume)
        scheduler_authority.reconcile_pending(allow_connect=False)
        assert jobs.get_job(job['id'])['enabled'] is True
        assert not journal.exists()


def test_existing_operator_hold_survives_unknown_and_retirement(tmp_path):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        held = jobs.pause_job(job['id'], reason='operator hold')
        fire, journal = _paused_fire(job)
        assert json.loads(journal.read_text())['pause_recorded'] is True
        executions.finish_execution(fire, success=False, error='stopped')
        scheduler_authority.reconcile_pending(allow_connect=False)
        saved = jobs.get_job(job['id'])
        assert saved['paused_at'] == held['paused_at']
        assert saved['paused_reason'] == 'operator hold'
        assert not saved['enabled']
        assert not journal.exists()


def test_repeated_unknown_keeps_its_original_pause_identity(tmp_path):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire, journal = _paused_fire(job)
        original = json.loads(journal.read_text())['pause_recorded']
        scheduler_authority.pause_for_unknown(job['id'], fire, 'second observer lost')
        assert json.loads(journal.read_text())['pause_recorded'] == original
        executions.finish_execution(fire, success=False, error='stopped')
        scheduler_authority.reconcile_pending(allow_connect=False)
        assert jobs.get_job(job['id'])['enabled'] is True


def test_conflicting_journal_is_retained(tmp_path):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire, journal = _paused_fire(job)
        record = json.loads(journal.read_text())
        record['params']['request_id'] = 'another-fire'
        journal.write_text(json.dumps(record))
        with pytest.raises(ValueError, match='identity conflict'):
            scheduler_authority.retire_terminal_journal(job['id'], fire)
        assert journal.exists()
        assert not jobs.get_job(job['id'])['enabled']
