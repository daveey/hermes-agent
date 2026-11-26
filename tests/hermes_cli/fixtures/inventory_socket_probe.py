"""Same-installed-ledger authenticated socket causal fixture, committed before boot."""
import asyncio
import faulthandler
import importlib
from importlib import _bootstrap
import json
import os
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

from gateway import code_provenance_control as pc
from gateway import quiet_control, webapp_listener
from gateway.run_bootstrap import _start_gateway_start_control_socket
from hermes_cli import code_attestation as ca
from hermes_cli.code_provenance_client import query


def run(ledger, root, sentinel, receipt):
    assert ca._ledger() is ca._installed is ledger
    webapp_listener.register_control_verbs = lambda *a, **kw: None
    quiet_control.register_quiet_control = lambda *a: None
    home = Path(os.environ['HERMES_HOME'])
    home.chmod(0o700)
    runner = SimpleNamespace(session_runtime_descriptor={'instance_id': 'inventory-fixture',
                             'state': 'ready', 'capabilities': []}, session_ticket_store=None)
    held, entered, audit_wait, release = (threading.Event() for _ in range(4))
    importer_done = threading.Event()
    getter_calls = []
    errors = []
    real_lock = ledger._lock
    import_lock = _bootstrap._get_module_lock('inventory_fixture_blocked')
    code = compile(sentinel.read_bytes(), str(sentinel), 'exec')

    class ObservedLock:
        def acquire(self, *args, **kwargs):
            return real_lock.acquire(*args, **kwargs)
        def release(self):
            return real_lock.release()
        def __enter__(self):
            if threading.current_thread().name == 'fixture-importer':
                audit_wait.set()  # Actual installed audit has entered its lock boundary.
            real_lock.acquire()
            return self
        def __exit__(self, *args):
            real_lock.release()

    ledger._lock = ObservedLock()
    module = ModuleType('inventory_fixture_lazy')
    def dynamic(name):
        getter_calls.append(name)
        assert audit_wait.wait(5)
        importlib.import_module('inventory_fixture_blocked')
    module.__getattr__ = dynamic
    def modules():
        entered.set()  # Called only after the real capture owns the ledger lock.
        assert audit_wait.wait(5)
        return (('inventory_fixture_lazy', module),)
    ledger._module_snapshot = modules
    def importer():
        try:
            import_lock.acquire()
            held.set()
            assert entered.wait(8)
            exec(code, {})
        except BaseException as exc:
            errors.append(type(exc).__name__)
        finally:
            import_lock.release()
            importer_done.set()
    worker = threading.Thread(target=importer, name='fixture-importer', daemon=True)

    async def scenario():
        server = await _start_gateway_start_control_socket(runner)
        assert server is not None
        control = server.private_handlers['code-provenance'].__self__
        assert control.runner is runner and ca._installed is ledger
        params = {'schema': 1, 'op': 'capture', 'expected': control.identity(), 'refresh_inventory': True}
        worker.start()
        assert await asyncio.to_thread(held.wait, 5)
        try:
            summary = await asyncio.to_thread(query, home, params, timeout=5)
            result = {**receipt, 'summary': summary, 'getter_calls': getter_calls,
                      'audit_wait': audit_wait.is_set(), 'inventory_entered': entered.is_set(),
                      'same_ledger': ca._installed is ledger, 'has_token': 'snapshot_id' in summary,
                      'controller_snapshots': len(control._snapshots)}
            if summary.get('error') == 'capture_timeout':
                # Parent terminates/reaps this owned deadlocked process after preserving receipt/stacks.
                result['old_deadlock'] = True
                faulthandler.dump_traceback()
                await asyncio.to_thread(Path(os.environ['ATTEST_PROBE_OUT']).write_text, json.dumps(result))
                await asyncio.to_thread(release.wait, 60)
                raise AssertionError('parent watchdog failed')
            assert await asyncio.to_thread(importer_done.wait, 5)
            worker.join(5)
            assert not worker.is_alive() and not errors, errors
            assert pc._FLIGHT.acquire(timeout=5)
            pc._FLIGHT.release()
            assert not control._snapshots
            ledger._module_snapshot = lambda: ()
            recovered = await asyncio.to_thread(query, home, params, timeout=5)
            result.update(importer_done=True, flight_recovered=True, recovered=recovered)
            return result
        finally:
            release.set()
            await server.stop()
            if importer_done.is_set():
                worker.join(5)
                ledger._lock = real_lock
    return asyncio.run(scenario())
