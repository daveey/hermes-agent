"""Boundary controls for exact-identity uncertainty hold retirement."""
import json
from pathlib import Path

import pytest

from cron import jobs, scheduler_authority as authority
from tests.cron.test_cron_terminal_pause_retirement import _paused_fire


@pytest.mark.parametrize('missing', ['job', 'journal'])
def test_missing_retirement_target_is_idempotent(tmp_path, missing):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire, journal = _paused_fire(job)
        if missing == 'job':
            jobs.remove_job(job['id'])
        else:
            journal.unlink()
        authority.retire_terminal_journal(job['id'], fire)
        authority.retire_terminal_journal(job['id'], fire)
        assert not journal.exists()


@pytest.mark.parametrize('damage', ['json', 'identity'])
def test_damaged_sibling_keeps_hold_and_recovery_link(tmp_path, damage):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire, journal = _paused_fire(job)
        _, sibling = _paused_fire(job)
        record = json.loads(sibling.read_text())
        record['params']['request_id'] = 'foreign'
        sibling.write_text('{' if damage == 'json' else json.dumps(record))
        with pytest.raises(ValueError):
            authority.retire_terminal_journal(job['id'], fire)
        assert journal.exists()
        assert not jobs.get_job(job['id'])['enabled']


def test_unrelated_job_does_not_own_this_hold(tmp_path):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        other = jobs.create_job(prompt='other', schedule='every 1h', deliver='local')
        fire, journal = _paused_fire(job)
        _, sibling = _paused_fire(other)
        record = json.loads(sibling.read_text())
        record['pause_recorded'] = json.loads(journal.read_text())['pause_recorded']
        sibling.write_text(json.dumps(record))
        authority.retire_terminal_journal(job['id'], fire)
        assert jobs.get_job(job['id'])['enabled']
        assert not jobs.get_job(other['id'])['enabled']
        assert sibling.exists()


def test_disappearing_sibling_is_not_a_hold(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire, journal = _paused_fire(job)
        _, sibling = _paused_fire(job)
        read = Path.read_text

        def disappearing(path, *args, **kwargs):
            if path == sibling:
                sibling.unlink()
                raise FileNotFoundError(str(path))
            return read(path, *args, **kwargs)

        monkeypatch.setattr(Path, 'read_text', disappearing)
        authority.retire_terminal_journal(job['id'], fire)
        assert jobs.get_job(job['id'])['enabled']
        assert not journal.exists()


def test_unknown_missing_job_records_non_owned_hold(tmp_path):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire, journal = _paused_fire(job)
        record = json.loads(journal.read_text())
        record.pop('pause_recorded')
        journal.write_text(json.dumps(record))
        jobs.remove_job(job['id'])
        authority.pause_for_unknown(job['id'], fire, 'unknown')
        assert json.loads(journal.read_text())['pause_recorded'] is True
        authority.retire_terminal_journal(job['id'], fire)
        assert jobs.get_job(job['id']) is None


def test_unknown_without_journal_does_not_invent_admission(tmp_path):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        authority.pause_for_unknown(job['id'], 'missing', 'unknown')
        assert not jobs.get_job(job['id'])['enabled']
        assert not authority.journal_path(job['id'], 'missing').exists()


def test_operator_pause_serializes_with_retirement(tmp_path, monkeypatch):
    import threading

    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire, journal = _paused_fire(job)
        attempted = threading.Event()
        finished = threading.Event()
        original = jobs.resume_job

        def operator():
            attempted.set()
            jobs.pause_job(job['id'], reason='operator hold')
            finished.set()

        from contextvars import copy_context
        context = copy_context()
        thread = threading.Thread(target=lambda: context.run(operator))

        def resume(job_id):
            thread.start()
            assert attempted.wait(5)
            assert not finished.is_set()
            return original(job_id)

        monkeypatch.setattr(jobs, 'resume_job', resume)
        authority.retire_terminal_journal(job['id'], fire)
        thread.join(5)
        assert not thread.is_alive()
        assert finished.is_set()
        assert jobs.get_job(job['id'])['paused_reason'] == 'operator hold'
        assert not jobs.get_job(job['id'])['enabled']
        assert not journal.exists()
