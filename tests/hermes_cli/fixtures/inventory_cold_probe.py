"""Cold sibling import under the installed hook, real capture worker and private socket."""
import asyncio
import faulthandler
import hashlib
import importlib
import importlib.machinery
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

from gateway import code_provenance_control as pc
from gateway import quiet_control, webapp_listener
from gateway.run_bootstrap import _start_gateway_start_control_socket
from hermes_cli import code_attestation as ca
from hermes_cli.code_provenance_client import query


def run(ledger, root, receipt, delayed):
    target = 'hermes_cli.code_attestation_inventory'
    assert target not in sys.modules  # Before either capture/importer is released.
    assert ca._ledger() is ca._installed is ledger
    webapp_listener.register_control_verbs = lambda *a, **kw: None
    quiet_control.register_quiet_control = lambda *a: None
    home = Path(os.environ['HERMES_HOME'])
    home.chmod(0o700)
    runner = SimpleNamespace(session_runtime_descriptor={'instance_id': 'cold-fixture', 'state': 'ready',
                             'capabilities': []}, session_ticket_store=None)
    loading, decision, release, done = (threading.Event() for _ in range(4))
    audit_wait = threading.Event()
    import_attempt = threading.Event()
    errors = []
    real_import, real_lock = importlib.import_module, ledger._lock
    before = ledger._generation
    unknown_before = set(ledger._unknown)
    ledger._module_snapshot = lambda: ()  # Controlled cold-order proof; not ambient inventory parity.

    class Lock:
        def acquire(self, *args, **kwargs):
            acquired = real_lock.acquire(*args, **kwargs)
            if acquired:
                decision.set()
            return acquired
        def release(self):
            return real_lock.release()
        def __enter__(self):
            if threading.current_thread().name == 'cold-importer':
                audit_wait.set()
            real_lock.acquire()
            return self
        def __exit__(self, *args):
            real_lock.release()
    ledger._lock = Lock()
    def observed_import(name, package=None):
        if name == target and threading.current_thread().name != 'cold-importer':
            import_attempt.set()
            decision.set()
        return real_import(name, package)
    importlib.import_module = observed_import

    class Finder:
        def find_spec(self, fullname, path, target_module=None):
            if fullname != target:
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            original = spec.loader
            class Loader:
                def create_module(self, spec):
                    return original.create_module(spec)
                def exec_module(self, module):
                    loading.set()
                    assert decision.wait(8)
                    if delayed:
                        assert release.wait(8)
                    original.exec_module(module)  # Authentic core source execution reaches installed audit.
            spec.loader = Loader()
            return spec
    finder = Finder()
    sys.meta_path.insert(0, finder)
    def importer():
        try:
            real_import(target)
        except BaseException as exc:
            errors.append(type(exc).__name__)
        finally:
            done.set()
    worker = threading.Thread(target=importer, name='cold-importer', daemon=True)

    async def scenario():
        server = await _start_gateway_start_control_socket(runner)
        control = server.private_handlers['code-provenance'].__self__
        assert ca._installed is ledger
        params = {'schema': 1, 'op': 'capture', 'expected': control.identity(), 'refresh_inventory': True}
        worker.start()
        assert await asyncio.to_thread(loading.wait, 5)
        try:
            summary = await asyncio.to_thread(query, home, params, timeout=5)
            result = {**receipt, 'summary': summary, 'cold_before': True,
                      'prelock_import_attempt': import_attempt.is_set(), 'audit_wait': audit_wait.is_set()}
            if not import_attempt.is_set():
                result['old_deadlock'] = True
                faulthandler.dump_traceback()
                await asyncio.to_thread(Path(os.environ['ATTEST_PROBE_OUT']).write_text, json.dumps(result))
                await asyncio.to_thread(release.wait, 60)
                raise AssertionError('parent watchdog failed')
            if delayed:
                assert summary['error'] == 'capture_timeout' and 'snapshot_id' not in summary
                assert summary['diagnostics']['phase'] == 'disk'
                assert 'inventory' not in summary['diagnostics']
                assert pc._FLIGHT.locked() and not control._snapshots
                busy = await asyncio.to_thread(query, home, params, timeout=5)
                assert busy['error'] == 'capture_busy'
                result['busy_before_release'] = True
                release.set()
            assert await asyncio.to_thread(done.wait, 5)
            worker.join(5)
            assert not worker.is_alive() and not errors, errors
            assert pc._FLIGHT.acquire(timeout=5)
            pc._FLIGHT.release()
            if delayed:
                assert not control._snapshots
            sibling = root / 'hermes_cli/code_attestation_inventory.py'
            digest = hashlib.sha256(sibling.read_bytes()).hexdigest()
            units = [u for u in ledger._units if u.get('filename') == str(sibling)]
            assert any(u.get('source_sha256') == digest for u in units), units
            assert ledger._generation > before
            assert ledger._unknown == unknown_before, ledger._unknown - unknown_before
            result.update(sibling_units=units, sibling_sha256=digest, before_generation=before,
                          after_generation=ledger._generation, joined=True, flight_recovered=True)
            return result
        finally:
            release.set()
            await server.stop()
            if done.is_set():
                worker.join(5)
                sys.meta_path.remove(finder)
                importlib.import_module = real_import
                ledger._lock = real_lock
    return asyncio.run(scenario())
