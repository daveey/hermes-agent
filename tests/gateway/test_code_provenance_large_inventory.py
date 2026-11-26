"""Real large module traversal isolated from the separate 16384-unit projection."""
import asyncio
import threading
from types import ModuleType

import pytest

from gateway import code_provenance_control as pc
from hermes_cli import code_attestation as ca
from tests.gateway import test_code_provenance_control as helpers

ledger = helpers.ledger
runtime = helpers.runtime
pytestmark = pytest.mark.platforms('linux', 'macos')


def test_large_real_inventory_is_not_masked_by_large_projection(runtime, monkeypatch):
    a = runtime[2]
    a._modules = {}
    modules = []
    for n in range(ca.MAX_UNITS):
        module = ModuleType(f'fixture_{n}')
        module.__file__ = str(a.core_root / 'plugin' / 'a.py')
        modules.append((module.__name__, module))
    monkeypatch.setattr(a, '_inventory_modules_diagnostic', ca.Attestation._inventory_modules_diagnostic.__get__(a))
    monkeypatch.setattr(a, '_module_snapshot', lambda: modules)
    original = a._module_file
    entered = threading.Event()
    seen, workers = [], []
    def inspect(name, filename):
        if not workers:
            workers.append(threading.current_thread())
            entered.set()
        original(name, filename)
        seen.append(name)
    monkeypatch.setattr(a, '_module_file', inspect)
    async def scenario():
        server, control, params = await helpers.start(runtime)
        try:
            pending = asyncio.create_task(helpers.ask(runtime, params))
            assert await asyncio.to_thread(entered.wait, 2)
            result = (await pending)['result']
            helpers.capture_result(result)
            if result['complete']:
                assert result['diagnostics']['phase'] == 'complete'
                assert len(seen) == len(a._modules) == 16384
            else:
                assert result['error'] == 'capture_timeout'
                assert result['diagnostics']['phase'] == 'inventory'
                assert not control._snapshots
            # Host speed is not a production timing oracle. Forced inventory timeout
            # is separately Event-gated; this case proves real capped graph traversal.
            assert seen and len(a._units) == 1
        finally:
            for worker in workers:
                await asyncio.to_thread(worker.join, 2)
                assert not worker.is_alive()
            assert not pc._FLIGHT.locked()
            await server.stop()
    asyncio.run(scenario())
