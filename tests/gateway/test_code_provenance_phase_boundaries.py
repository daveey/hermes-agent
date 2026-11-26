"""Production phase boundaries through authenticated Unix sockets; no live state."""
import asyncio
import json
import threading

import pytest

from gateway import code_provenance_control as pc
from hermes_cli import code_attestation as ca
from hermes_cli import code_attestation_snapshot as projection
from hermes_cli import code_provenance_progress as pp
from tests.gateway import test_code_provenance_control as helpers

ledger = helpers.ledger
runtime = helpers.runtime
pytestmark = pytest.mark.platforms('linux', 'macos')


@pytest.mark.parametrize('phase', ['worker_start', 'disk', 'ledger_lock', 'inventory',
                                  'registry', 'projection', 'finalize', 'complete'])
def test_real_boundary_timeout_old_pages_busy_join_and_recovery(runtime, monkeypatch, phase):
    entered, release = threading.Event(), threading.Event()
    workers, holders = [], []
    async def scenario():
        server, control, params = await helpers.start(runtime)
        try:
            previous = (await helpers.ask(runtime, params))['result']
            original = ca.diagnostic_snapshot
            def gate():
                workers.append(threading.current_thread())
                entered.set()
                assert release.wait(8)
            with monkeypatch.context() as patch:
                if phase == 'worker_start':  # health: allow HX011 -- distinct production seams require different fixture signatures and cleanup; keep each causal gate explicit
                    def start_gate(**kwargs):
                        gate()
                        return original(**kwargs)
                    patch.setattr(ca, 'diagnostic_snapshot', start_gate)
                elif phase == 'disk':
                    disk = projection.disk_identity
                    def disk_gate(*args):
                        gate()
                        return disk(*args)
                    patch.setattr(projection, 'disk_identity', disk_gate)
                elif phase == 'ledger_lock':
                    def hold():
                        with runtime[2]._lock:
                            entered.set()
                            assert release.wait(8)
                    holder = threading.Thread(target=hold)
                    holders.append(holder)
                    holder.start()
                    assert await asyncio.to_thread(entered.wait, 2)
                    entered.clear()
                    disk = projection.disk_identity
                    def signal_disk(*args):
                        result = disk(*args)
                        workers.append(threading.current_thread())
                        entered.set()
                        return result
                    patch.setattr(projection, 'disk_identity', signal_disk)
                elif phase == 'inventory':
                    patch.setattr(runtime[2], '_inventory_modules_diagnostic', lambda *, deadline=None, progress=None: gate())
                elif phase == 'registry':
                    from hermes_cli import plugin_review_records
                    runtime[2]._reviewed_plugins = {'fixture': True}
                    patch.setattr(plugin_review_records, 'verify_registered', lambda ledger: gate())
                elif phase == 'projection':
                    metadata = projection._metadata
                    def metadata_gate(*args):
                        gate()
                        return metadata(*args)
                    patch.setattr(projection, '_metadata', metadata_gate)
                elif phase == 'finalize':
                    finish = projection._Builder.finish
                    def finish_gate(*args):
                        gate()
                        return finish(*args)
                    patch.setattr(projection._Builder, 'finish', finish_gate)
                else:
                    sample = pp.CaptureProgress.set_phase
                    def complete_gate(self, value):
                        sample(self, value)
                        if value == 'complete':
                            gate()
                    patch.setattr(pp.CaptureProgress, 'set_phase', complete_gate)
                pending = asyncio.create_task(helpers.ask(runtime, params))
                assert await asyncio.to_thread(entered.wait, 2)
                busy = (await helpers.ask(runtime, params))['result']
                assert busy == pc._error('capture_busy') and 'diagnostics' not in busy
                assert (await helpers.ask(runtime, helpers.page_params(previous)))['result']['complete']
                result = (await pending)['result']
                assert helpers.capture_result(result) == pc._error('capture_timeout')
                assert result['diagnostics']['phase'] == phase
                frozen = json.dumps(result, sort_keys=True)
                assert 'snapshot_id' not in result
                assert next(iter(control._snapshots.values()))['token'] == previous['snapshot_id']
                release.set()
                for worker in workers + holders:
                    await asyncio.to_thread(worker.join, 2)
                    assert not worker.is_alive()
                assert not pc._FLIGHT.locked()
                assert json.dumps(result, sort_keys=True) == frozen
                assert next(iter(control._snapshots.values()))['token'] == previous['snapshot_id']
            runtime[2]._reviewed_plugins = {}
            fresh = (await helpers.ask(runtime, params))['result']
            assert fresh['complete'] and fresh['diagnostics']['phase'] == 'complete'
        finally:
            release.set()
            for worker in workers + holders:
                await asyncio.to_thread(worker.join, 2)
            await server.stop()
    asyncio.run(scenario())


def test_recorder_lock_cannot_extend_socket_timeout(runtime, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    workers, recorders = [], []
    original = ca.diagnostic_snapshot
    def locked(*, deadline, progress):
        workers.append(threading.current_thread())
        recorders.append(progress)
        with progress._lock:
            entered.set()
            assert release.wait(8)
        return original(deadline=deadline, progress=progress)
    monkeypatch.setattr(ca, 'diagnostic_snapshot', locked)
    async def scenario():
        server, control, params = await helpers.start(runtime)
        try:
            pending = asyncio.create_task(helpers.ask(runtime, params))
            assert await asyncio.to_thread(entered.wait, 2)
            result = (await asyncio.wait_for(pending, 4))['result']
            assert helpers.capture_result(result) == pc._error('capture_timeout')
            assert result['diagnostics'] == {**pp.unavailable(), 'inventory': {
                'schema': 1, 'stage': 'unavailable', 'completed': 0, 'sampled_elapsed_ms': 0,
                'worker_cpu_ms': 0, 'cpu_is_lower_bound': True}}
            assert pc._FLIGHT.locked() and not control._snapshots
        finally:
            release.set()
            for worker in workers:
                await asyncio.to_thread(worker.join, 2)
                assert not worker.is_alive()
            await server.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['startup', 'observer', 'snapshot'])
@pytest.mark.parametrize('failure', [LookupError, ArithmeticError])
def test_recorder_failure_sanitized_no_publication_or_hook_leak(runtime, monkeypatch, stage, failure):
    hooks, workers = [], []
    monkeypatch.setattr(threading, 'excepthook', hooks.append)
    async def scenario():
        server, control, params = await helpers.start(runtime)
        try:
            old = (await helpers.ask(runtime, params))['result']
            with monkeypatch.context() as patch:
                def fail(*args, **kwargs):
                    if threading.current_thread().name == 'gateway-code-provenance':
                        workers.append(threading.current_thread())
                    raise failure('RECORDER_SECRET_SENTINEL')
                if stage == 'startup':
                    patch.setattr(pc, 'CaptureProgress', fail)
                elif stage == 'observer':
                    patch.setattr(pp.CaptureProgress, 'set_phase', fail)
                else:
                    patch.setattr(pp.CaptureProgress, 'snapshot', fail)
                result = (await helpers.ask(runtime, params))['result']
                assert helpers.capture_result(result) == pc._error('capture_failed')
                assert 'RECORDER_SECRET_SENTINEL' not in json.dumps(result)
                assert not pc._FLIGHT.locked()
                for worker in workers:
                    await asyncio.to_thread(worker.join, 2)
                    assert not worker.is_alive()
                assert not hooks
                assert next(iter(control._snapshots.values()))['token'] == old['snapshot_id']
                assert (await helpers.ask(runtime, helpers.page_params(old)))['result']['complete']
            assert (await helpers.ask(runtime, params))['result']['complete']
        finally:
            await server.stop()
    asyncio.run(scenario())


def test_unadmitted_requests_never_allocate_recorder(runtime, monkeypatch):
    def forbidden():
        pytest.fail('unadmitted capture allocated recorder')
    monkeypatch.setattr(pc, 'CaptureProgress', forbidden)
    control = pc.CodeProvenanceControl(runtime[1])
    params = {'schema': 1, 'op': 'capture', 'expected': control.identity(), 'refresh_inventory': True}
    assert control.handle({**params, 'expected': {**params['expected'], 'pid': 1}}, 'peer') == pc._error('identity_mismatch')
    with pytest.raises(PermissionError):
        control.handle(params, None)
    with pytest.raises(ValueError):
        control.handle({**params, 'schema': 2}, 'peer')
    pc._FLIGHT.acquire()
    try:
        assert control.handle(params, 'peer') == pc._error('capture_busy')
    finally:
        pc._FLIGHT.release()
