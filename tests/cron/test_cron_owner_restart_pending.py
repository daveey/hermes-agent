"""A replacement owner runs only queued admissions, never an interrupted started fire."""
from contextlib import asynccontextmanager

import pytest

from cron import executions, jobs, scheduler
from gateway import session_cron
from hermes_cli import gateway_client
from hermes_state_runtime import claim_session_input, recover_session_inputs, settle_session_input
from tests.cron import test_cron_recovery as helpers


@pytest.mark.parametrize('started', [False, True], ids=['queued', 'unknown-started'])
def test_restart_pending_admission_never_resubmits(tmp_path, monkeypatch, started):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='controlled pending fire', schedule='every 1h', deliver='local')
        owner, db = helpers._owner(tmp_path, monkeypatch)
        active, databases, calls, connections = [owner], [db], [], []
        fire = executions.create_execution(job['id'], source='test')['id']
        monkeypatch.setattr('cron.observer_reconnect.RECONNECT_DELAY', 0)

        class Peer:
            async def rpc(self, method, **params):
                op = method.removeprefix('cron.')
                calls.append(op)
                if len(connections) == 1 and op == 'status':
                    databases[-1].close()
                    replacement, replacement_db = helpers._owner(tmp_path, monkeypatch)
                    active.append(replacement)
                    databases.append(replacement_db)
                    recover_session_inputs(replacement_db, epoch=replacement.epoch)
                    raise gateway_client.GatewayClientError(gateway_client.DISCONNECTED)
                answer = await session_cron.operation(active[-1], op, params)
                if op == 'submit' and started:
                    claim_session_input(databases[-1], epoch=active[-1].epoch, session_id=answer['session_id'])
                if op == 'recover' and not started:
                    assert answer['status'] == 'queued'
                    row = claim_session_input(databases[-1], epoch=active[-1].epoch, session_id=answer['session_id'])
                    settle_session_input(databases[-1], epoch=active[-1].epoch,
                                         admission_id=row['admission_id'], generation=row['generation'],
                                         outcome='completed',
                                         result={'result': {'cron_result': [True, 'doc', 'answer', None]}, 'usage': {}})
                return answer

        @asynccontextmanager
        async def connect():
            connections.append(Peer())
            yield connections[-1]

        monkeypatch.setattr(gateway_client, 'connect_gateway', connect)
        try:
            scheduler._run_one_job_body(dict(jobs.get_job(job['id']), execution_id=fire))
            assert calls.count('submit') == 1
            assert calls.count('recover') == 1
            assert len(connections) == 2
            current = jobs.get_job(job['id'])
            if started:
                assert current['state'] == 'paused'
                assert executions.get_execution(fire)['status'] == 'unknown'
            else:
                assert current['last_status'] == 'ok'
                assert executions.get_execution(fire)['status'] == 'completed'
        finally:
            databases[-1].close()
