"""Real owner/admission/execution stores across replacement of the canonical owner.

The transport drop is injected, not a live service restart. The workload is a
controlled local result; no model, external side effects or production jobs run.
"""
from contextlib import asynccontextmanager

import pytest

from cron import executions, jobs, scheduler
from gateway import session_cron
from gateway.session_contract import SessionRef
from hermes_cli import gateway_client
from hermes_state_runtime import claim_session_input, settle_session_input
from tests.cron import test_cron_recovery as helpers


@pytest.mark.parametrize('success', [True, False], ids=['completed', 'shutdown-interrupted'])
def test_firer_books_durable_terminal_after_owner_replacement(tmp_path, monkeypatch, success):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='controlled restart proof', schedule='every 1h', deliver='local')
        owner, db = helpers._owner(tmp_path, monkeypatch)
        owners, databases, connections, submissions = [owner], [db], [], []
        fire = executions.create_execution(job['id'], source='test')['id']
        original_run = scheduler.run_job
        expected = (True, 'document', 'answer', None) if success else (
            False, 'interrupted document', '', 'Gateway shutdown interrupted the controlled workload')

        def workload(job, **kwargs):
            if session_cron.current_execution() is not None:
                return expected
            return original_run(job, **kwargs)

        monkeypatch.setattr(scheduler, 'run_job', workload)
        monkeypatch.setattr('cron.observer_reconnect.RECONNECT_DELAY', 0)

        class Peer:
            async def rpc(self, method, **params):
                op = method.removeprefix('cron.')
                if len(connections) == 1 and op == 'status':
                    # The owner has settled, but its final reply never reaches the
                    # external firer. A fresh owner reads the same durable receipt.
                    databases[-1].close()
                    replacement, replacement_db = helpers._owner(tmp_path, monkeypatch)
                    owners.append(replacement)
                    databases.append(replacement_db)
                    raise gateway_client.GatewayClientError(gateway_client.DISCONNECTED)
                answer = await session_cron.operation(owners[-1], op, params)
                if op == 'submit':
                    submissions.append(params)
                    active = owners[-1]
                    row = claim_session_input(databases[-1], epoch=active.epoch,
                                              session_id=answer['session_id'])
                    policy = next(iter(active.runner.adapters.values())).policies[answer['session_id']]
                    try:
                        await session_cron.execute(active, SessionRef(active.profile_id, answer['session_id']), row, policy)
                    except RuntimeError:
                        if success:
                            raise
                    settle_session_input(databases[-1], epoch=active.epoch,
                                         admission_id=row['admission_id'], generation=row['generation'],
                                         outcome='completed' if success else 'failed',
                                         result=active.pending_results.pop(row['admission_id']))
                return answer

        @asynccontextmanager
        async def connect():
            connections.append(Peer())
            yield connections[-1]

        monkeypatch.setattr(gateway_client, 'connect_gateway', connect)
        try:
            scheduler._run_one_job_body(dict(jobs.get_job(job['id']), execution_id=fire))
            assert len(connections) == 2
            assert len(submissions) == 1
            assert owners[0].epoch != owners[1].epoch
            assert executions.get_execution(fire)['status'] == ('completed' if success else 'failed')
            assert jobs.get_job(job['id'])['last_status'] == ('ok' if success else 'error')
            assert jobs.get_job(job['id'])['state'] != 'paused'
            assert executions.recover_interrupted_executions() == 0
        finally:
            databases[-1].close()
