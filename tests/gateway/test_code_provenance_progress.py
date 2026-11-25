"""Designer acceptance specification, exercised and extended against real transport."""
import asyncio
import json
import threading


import pytest
from hermes_cli import code_attestation as ca
from tests.gateway import test_code_provenance_control as helpers

ledger = helpers.ledger
runtime = helpers.runtime
pytestmark = pytest.mark.platforms('linux', 'macos')


def diagnostic(result):
    helpers.capture_result(result)
    d = result['diagnostics']
    assert d['schema'] == 1
    assert d['phase'] in {'worker_start', 'disk', 'ledger_lock', 'inventory', 'registry', 'projection', 'finalize', 'complete', 'unavailable'}
    assert type(d['sampled_elapsed_ms']) is int and d['sampled_elapsed_ms'] >= 0
    assert type(d['worker_cpu_ms']) is int and d['worker_cpu_ms'] >= 0
    assert d['cpu_is_lower_bound'] is True
    assert len(json.dumps(d).encode()) <= 4096
    return d


def test_authenticated_complete_has_bounded_diagnostic_only_telemetry(runtime):
    async def scenario():
        server, _control, params = await helpers.start(runtime)
        try:
            result = (await helpers.ask(runtime, params))['result']
            assert result['complete'] is True
            assert result['full_provenance'] is False
            assert diagnostic(result)['phase'] == 'complete'
            assert (await helpers.ask(runtime, helpers.page_params(result)))['result']['snapshot_id'] == result['snapshot_id']
        finally:
            await server.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['disk', 'inventory', 'registry', 'projection', 'finalize'])
def test_timeout_phase_is_frozen_and_late_result_unpublishable(runtime, monkeypatch, phase):
    # Proposed Progress interface from the report: set_phase(enum), snapshot().
    # Force the non-cooperative point only in the disposable snapshot function.
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    workers = []
    original = ca.diagnostic_snapshot
    def delayed(*, deadline, progress=None):
        workers.append(threading.current_thread())
        assert progress is not None
        progress.set_phase(phase)
        entered.set()
        assert release.wait(8)
        try:
            # The actual expired original must not mint a successful snapshot.
            return original(deadline=deadline)
        finally:
            progress.set_phase('unavailable')
            finished.set()
    monkeypatch.setattr(ca, 'diagnostic_snapshot', delayed)
    async def scenario():
        server, control, params = await helpers.start(runtime)
        try:
            pending = asyncio.create_task(helpers.ask(runtime, params))
            assert await asyncio.to_thread(entered.wait, 2)
            result = (await pending)['result']
            assert result['complete'] is False and result['error'] == 'capture_timeout'
            frozen = json.dumps(result, sort_keys=True)
            assert diagnostic(result)['phase'] == phase
            busy = (await helpers.ask(runtime, params))['result']
            assert busy['error'] == 'capture_busy'
            assert 'diagnostics' not in busy  # Never disclose another flight's telemetry.
            assert not control._snapshots
            release.set()
            assert await asyncio.to_thread(finished.wait, 2)
            await asyncio.to_thread(workers[0].join, 2)
            assert not workers[0].is_alive()
            # Worker terminal custody must also finish before testing recovery.
            from gateway import code_provenance_control as pc
            def wait_flight():
                assert pc._FLIGHT.acquire(timeout=2)
                pc._FLIGHT.release()
            await asyncio.to_thread(wait_flight)
            assert json.dumps(result, sort_keys=True) == frozen
            assert not control._snapshots
            monkeypatch.setattr(ca, 'diagnostic_snapshot', original)
            recovered = (await helpers.ask(runtime, params))['result']
            assert recovered['complete'] is True
        finally:
            release.set()
            for worker in workers:
                await asyncio.to_thread(worker.join, 2)
            await server.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [LookupError('SECRET_SENTINEL'), ArithmeticError('SECRET_SENTINEL')])
def test_ordinary_failure_keeps_reviewed_sanitized_settlement(runtime, monkeypatch, failure):
    def failed(**kwargs):
        raise failure
    monkeypatch.setattr(ca, 'diagnostic_snapshot', failed)
    async def scenario():
        server, control, params = await helpers.start(runtime)
        try:
            result = (await helpers.ask(runtime, params))['result']
            assert result['error'] == 'capture_failed' and result['complete'] is False
            diagnostic(result)
            assert 'SECRET_SENTINEL' not in json.dumps(result)
            assert not control._snapshots
        finally:
            await server.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['instance', 'ledger'])
def test_identity_change_between_snapshot_and_publication_never_mints_token(runtime, monkeypatch, change):
    entered, release = threading.Event(), threading.Event()
    original = ca.diagnostic_snapshot
    def gated(*, deadline, progress=None):
        snapshot = original(deadline=deadline)
        entered.set()
        assert release.wait(4)
        return snapshot
    monkeypatch.setattr(ca, 'diagnostic_snapshot', gated)
    async def scenario():
        server, control, params = await helpers.start(runtime)
        try:
            pending = asyncio.create_task(helpers.ask(runtime, params))
            assert await asyncio.to_thread(entered.wait, 1)
            if change == 'instance':
                runtime[1].session_runtime_descriptor['instance_id'] = 'replacement-instance'
            else:
                monkeypatch.setattr(ca, '_installed', object())
            release.set()
            result = (await pending)['result']
            assert result['error'] == 'identity_changed'
            assert result['complete'] is False and 'snapshot_id' not in result
            diagnostic(result)
            assert not control._snapshots
        finally:
            release.set()
            await server.stop()
    asyncio.run(scenario())


def test_large_projection_through_authenticated_client_retains_every_row(runtime):
    from hermes_cli.code_provenance_client import collect
    runtime[2]._units = [{'component': 'core', 'source_sha256': 'f' * 64,
                          'generation': n} for n in range(16384)]
    async def scenario():
        server, control, _params = await helpers.start(runtime)
        try:
            document = await asyncio.to_thread(collect, runtime[0], control.identity())
            assert diagnostic(document['summary'])['phase'] == 'complete'
            assert document['summary']['full_provenance'] is False
            assert len(document['sections']['executed_units']) == 16384
            assert document['summary']['sections']['executed_units']['count'] == 16384
            assert set(document['sections']['unknowns']) == runtime[2]._unknown
        finally:
            await server.stop()
    asyncio.run(scenario())
