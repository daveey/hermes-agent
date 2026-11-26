"""Committed final-dashboard probe; never constructs a ledger or installs an audit hook."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
from types import ModuleType

from hermes_cli import code_attestation as ca

ledger = ca._ledger()
report = ledger.route_report()
assert report['route'] == 'managed-stub', report
assert 'bootstrap-provenance-pending' not in ledger._unknown, ledger._unknown
assert any(s['kind'] == 'trusted_launcher_script' for s in ledger.trusted_bootstrap_seed())
root = Path(__file__).resolve().parent
sentinel = root / 'inventory_sentinel.py'
generation = ledger._generation
exec(compile(sentinel.read_bytes(), str(sentinel), 'exec'), {})
assert ledger._generation > generation
assert any(u.get('filename') == str(sentinel) and u.get('source_sha256') == hashlib.sha256(sentinel.read_bytes()).hexdigest() for u in ledger._units)
mode = os.environ['INVENTORY_PROBE_MODE']
if mode in ('socket', 'cold', 'cold_timeout'):
    receipt = {'route': report, 'fixture_head': ledger.boot_sha,
               'sentinel_generation': generation, 'interpreter': sys.executable}
    if mode == 'socket':
        from inventory_socket_probe import run
        result = run(ledger, root, sentinel, receipt)
    else:
        from inventory_cold_probe import run
        result = run(ledger, root, receipt, delayed=mode == 'cold_timeout')
    with open(os.environ['ATTEST_PROBE_OUT'], 'w') as stream:
        json.dump(result, stream)
    raise SystemExit(0)
importlib.import_module('hermes_cli.code_attestation_inventory')
module = ModuleType('ordinary')
module.__file__ = str(root / 'hermes_cli/code_attestation_snapshot.py')
modules = (('ordinary', module), ('placeholder', None))
ledger._module_snapshot = lambda: modules
retained = ledger._modules
before = ledger._generation
with ledger._lock:
    ledger._inventory_modules_diagnostic()
    assert ledger._modules is retained
    assert retained[('ordinary', id(module))][0] is module
    assert retained[('ordinary', id(module))][1] == module.__file__
    assert retained[('placeholder', id(None))] == (None, None)
    assert ledger._generation == before + 2
    ledger._inventory_modules_diagnostic()
    assert ledger._generation == before + 2
result = {'mode': mode, 'route': report, 'sentinel_generation': generation,
          'fixture_head': ledger.boot_sha, 'interpreter': sys.executable,
          'generation': ledger._generation, 'modules': len(retained)}
with open(os.environ['ATTEST_PROBE_OUT'], 'w') as stream:
    json.dump(result, stream)
