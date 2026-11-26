"""Diagnostic raw metadata: callbacks are not metadata or trust evidence."""
import importlib
from types import ModuleType, SimpleNamespace
from pathlib import Path
import sys
import os
import json
import subprocess

from tests.waiting import wait_until

from tests.hermes_cli import test_code_attestation_route as route_helpers

import pytest


@pytest.fixture(scope='module')
def diagnostic_core(tmp_path_factory):
    # Separate committed factory variant, never mutate the shared route fixture.
    root = route_helpers.core.__wrapped__(tmp_path_factory)
    probe = Path(__file__).parent / 'fixtures/inventory_diagnostic_probe.py'
    (root / 'attest_probe.py').write_bytes(probe.read_bytes())
    for name in ('inventory_socket_probe.py', 'inventory_cold_probe.py'):
        (root / name).write_bytes((probe.parent / name).read_bytes())
    (root / 'inventory_sentinel.py').write_text('sentinel_executed = True\n')
    route_helpers._git(root, 'add', '-A')
    route_helpers._git(root, 'commit', '-qm', 'diagnostic fixture closure')
    return root


@pytest.mark.live_system_guard_bypass
def test_supported_whole_inventory_preserves_retained_map_and_identity(diagnostic_core, tmp_path):
    probe = route_helpers.run_stub(tmp_path, Path(sys.executable), route_helpers.current_script(diagnostic_core),
                                   extra_env={'INVENTORY_PROBE_MODE': 'ordinary'})
    assert probe['mode'] == 'ordinary'
    assert probe['route']['route'] == 'managed-stub'
    assert probe['generation'] > probe['sentinel_generation']
    assert probe['fixture_head'] == route_helpers._git(diagnostic_core, 'rev-parse', 'HEAD').strip()


def _supported_socket_probe(diagnostic_core, tmp_path, mode):
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    output = tmp_path / 'probe.json'
    argv = [sys.executable, '-I', '-c', route_helpers.current_script(diagnostic_core), *route_helpers.ARGS]
    env = {'PATH': os.defpath, 'HOME': str(tmp_path), 'TMPDIR': str(tmp_path), 'HERMES_HOME': str(home),
           'ATTEST_PROBE_OUT': str(output), 'INVENTORY_PROBE_MODE': mode, 'LANG': 'C.UTF-8'}
    process = subprocess.Popen(argv, cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    receipts = []
    def receipt_ready():
        if output.exists():
            try:
                receipts.append(json.loads(output.read_text()))
                return True
            except json.JSONDecodeError:
                pass
        return process.poll() is not None
    try:
        wait_until(receipt_ready, timeout=40)
        if not receipts:
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f'fixture exited before receipt: {process.returncode}: {stdout[-1000:]} {stderr[-3000:]}')
        result = receipts[0]
    finally:
        if process.poll() is None:
            process.terminate()
        stdout, stderr = process.communicate(timeout=10)
    print(json.dumps({'receipt': result, 'argv': argv, 'stderr': stderr, 'returncode': process.returncode}))
    return result


@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize('delayed', [False, True])
def test_supported_cold_import_precedes_ledger_lock_with_audited_source_receipt(diagnostic_core, tmp_path, delayed):
    result = _supported_socket_probe(diagnostic_core, tmp_path, 'cold_timeout' if delayed else 'cold')
    assert result['cold_before'] and result['prelock_import_attempt'], result
    assert result['joined'] and result['flight_recovered']
    assert result['after_generation'] > result['before_generation']
    if delayed:
        assert result['summary']['error'] == 'capture_timeout' and result['busy_before_release']
    else:
        assert result['summary']['complete'] is True


@pytest.mark.live_system_guard_bypass
def test_supported_socket_refuses_dynamic_metadata_and_recovers_flight(diagnostic_core, tmp_path):
    result = _supported_socket_probe(diagnostic_core, tmp_path, 'socket')
    assert result['same_ledger'] and result['audit_wait'] and result['inventory_entered']
    assert result['summary']['error'] == 'module_metadata_unavailable', result
    assert result['summary']['complete'] is False
    assert result['getter_calls'] == []
    assert not result['has_token'] and result['controller_snapshots'] == 0
    assert result['importer_done'] and result['flight_recovered']
    assert result['recovered']['complete'] is True


def test_inventory_sequence_and_completed_count_include_unchanged_and_placeholder():
    from hermes_cli import code_attestation_inventory as inv
    from hermes_cli.code_provenance_progress import CaptureProgress
    module = ModuleType('ordinary')
    observations = []
    class Observer(CaptureProgress):
        def set_inventory(self, stage, completed):
            super().set_inventory(stage, completed)
            observations.append((stage, completed))
    progress = Observer()
    progress.set_phase('worker_start')
    ledger = SimpleNamespace(_modules={('ordinary', id(module)): (module, None)}, _generation=7,
        _module_snapshot=lambda: (('ordinary', module), ('placeholder', None)),
        _module_file=lambda name, filename: None)
    inv.inventory_modules(ledger, progress=progress)
    assert observations == [('module_snapshot', 0), ('module_metadata', 0), ('module_file', 0),
        ('module_file', 1), ('module_metadata', 1), ('module_file', 1), ('module_file', 2), ('finished', 2)]
    assert ledger._generation == 8


@pytest.mark.parametrize('name', [object(), 1, None])
def test_inventory_refuses_nonexact_name_before_file_or_retention(name):
    from hermes_cli import code_attestation_inventory as inv
    calls = []
    ledger = SimpleNamespace(_modules={}, _generation=7,
        _module_snapshot=lambda: ((name, None),),
        _module_file=lambda *args: calls.append(args))
    with pytest.raises(ValueError, match='^module_metadata_unavailable$'):
        inv.inventory_modules(ledger)
    assert not calls and ledger._modules == {} and ledger._generation == 7


def test_dynamic_absent_file_refuses_without_invoking_getter():
    inventory = importlib.import_module('hermes_cli.code_attestation_inventory')
    module = ModuleType('lazy')
    called = []
    module.__getattr__ = lambda name: called.append(name)
    with pytest.raises(ValueError, match='^module_metadata_unavailable$'):
        inventory._module_filename(module, deadline=None, budget=[0])
    assert called == []


class HostileString(str):
    def __hash__(self):
        raise AssertionError('hash callback')

    def __eq__(self, other):
        raise AssertionError('equality callback')


class HostileModule(ModuleType):
    def __getattribute__(self, name):
        raise AssertionError('attribute callback')


class HostileFile:
    def __fspath__(self):
        raise AssertionError('path callback')

    def __str__(self):
        raise AssertionError('string callback')


def _metadata_module(key, value):
    module = ModuleType('ordinary')
    module.__dict__[key] = value
    return module


def _colliding_module(key_name='__file__'):
    class Key(str):
        armed = False
        def __hash__(self):
            if self.armed:
                raise AssertionError('hash callback')
            return str.__hash__(self)
        def __eq__(self, other):
            if self.armed:
                raise AssertionError('equality callback')
            return str.__eq__(self, other)
    key = Key(key_name)
    module = _metadata_module(key, 'source.py')
    key.armed = True
    return module


@pytest.mark.parametrize('factory', [lambda: HostileModule('subclass'), HostileFile,
    lambda: _metadata_module('__file__', HostileFile()),
    lambda: _metadata_module('__file__', HostileString('source.py')),
    _colliding_module, lambda: _colliding_module('__getattr__'), lambda: _metadata_module(123, None)])
def test_opaque_metadata_refuses_without_callbacks(factory):
    inventory = importlib.import_module('hermes_cli.code_attestation_inventory')
    module = factory()
    with pytest.raises(ValueError, match='^module_metadata_unavailable$'):
        inventory._module_filename(module, deadline=None, budget=[0])


@pytest.mark.parametrize('filename', [None, '', 'source.py'])
def test_exact_metadata_and_placeholders(filename):
    inventory = importlib.import_module('hermes_cli.code_attestation_inventory')
    module = ModuleType('ordinary')
    module.__file__ = filename
    budget = [0]
    assert inventory._module_filename(module, deadline=None, budget=budget) == filename
    assert budget == [6]
    assert inventory._module_filename(None, deadline=None, budget=budget) is None
    assert budget == [6]
    if filename is not None:
        module.__getattr__ = lambda name: pytest.fail('concrete metadata invoked getter')
        assert inventory._module_filename(module, deadline=None, budget=[0]) == filename


def test_metadata_budgets_exact_boundary_and_charge_unknown_keys(monkeypatch):
    inventory = importlib.import_module('hermes_cli.code_attestation_inventory')
    monkeypatch.setattr(inventory, 'MAX_MODULE_NAMESPACE_ITEMS', 6)
    monkeypatch.setattr(inventory, 'MAX_INVENTORY_NAMESPACE_ITEMS', 12)
    module = ModuleType('ordinary')
    module.__file__ = 'source.py'
    budget = [0]
    assert inventory._module_filename(module, deadline=None, budget=budget) == 'source.py'
    assert inventory._module_filename(module, deadline=None, budget=budget) == 'source.py'
    assert budget == [12]
    with pytest.raises(ValueError, match='^module_metadata_unavailable$'):
        inventory._module_filename(module, deadline=None, budget=budget)
    assert budget == [13]
    module.padding = None
    with pytest.raises(ValueError, match='^module_metadata_unavailable$'):
        inventory._module_filename(module, deadline=None, budget=[0])


def test_deadline_before_namespace_and_each_item(monkeypatch):
    inventory = importlib.import_module('hermes_cli.code_attestation_inventory')
    from hermes_cli import code_attestation as ca
    with pytest.raises(TimeoutError):
        inventory._module_filename(ModuleType('ordinary'), deadline=0, budget=[0])
    checks = []
    def check(deadline):
        checks.append(deadline)
        if len(checks) == 4:
            raise TimeoutError('controlled item deadline')
    monkeypatch.setattr(ca, '_check_inventory_deadline', check)
    budget = [0]
    with pytest.raises(TimeoutError, match='controlled item deadline'):
        inventory._module_filename(ModuleType('ordinary'), deadline=10, budget=budget)
    assert budget == [2]


class TupleSubclass(tuple):
    pass


class IntSubclass(int):
    pass


class DictSubclass(dict):
    def items(self):
        raise AssertionError('mapping callback')


@pytest.mark.parametrize('kind', ['map', 'key_type', 'key_length', 'name', 'identity', 'bool_identity',
                                  'value_type', 'value_length', 'filename'])
def test_retained_preflight_rejects_malformed_shapes_without_mutation(kind):
    inventory = importlib.import_module('hermes_cli.code_attestation_inventory')
    module = HostileFile()
    key, value = ('module', id(module)), (module, None)
    class Name(str):
        pass
    keys = {'key_type': TupleSubclass(key), 'key_length': ('module',),
            'name': (Name('module'), id(module)), 'identity': ('module', IntSubclass(1)),
            'bool_identity': ('module', True)}
    values = {'value_type': TupleSubclass(value), 'value_length': (module,),
              'filename': (module, HostileString('source.py'))}
    key, value = keys.get(kind, key), values.get(kind, value)
    retained = DictSubclass({key: value}) if kind == 'map' else {key: value}
    ledger = SimpleNamespace(_modules=retained)
    with pytest.raises(ValueError, match='^module_metadata_unavailable$'):
        inventory._retained_modules(ledger, deadline=None)
    assert ledger._modules is retained
    assert next(iter(dict.values(retained))) is value


def test_retained_preflight_is_inert_detached_and_bounded(monkeypatch):
    inventory = importlib.import_module('hermes_cli.code_attestation_inventory')
    from hermes_cli import code_attestation as ca
    module = HostileModule('retained')
    value = (module, 'source.py')
    retained = {('module', id(module)): value}
    ledger = SimpleNamespace(_modules=retained)
    copied = inventory._retained_modules(ledger, deadline=None)
    assert copied is not retained
    assert copied[('module', id(module))] is value
    monkeypatch.setattr(ca, 'MAX_UNITS', 1)
    assert len(inventory._retained_modules(ledger, deadline=None)) == 1
    retained[('other', 2)] = (None, None)
    with pytest.raises(ValueError, match='^module_metadata_unavailable$'):
        inventory._retained_modules(ledger, deadline=None)
    assert len(retained) == 2


def test_retained_preflight_mutation_and_deadline(monkeypatch):
    inventory = importlib.import_module('hermes_cli.code_attestation_inventory')
    from hermes_cli import code_attestation as ca
    retained = {('one', 1): (None, None), ('two', 2): (None, None)}
    ledger = SimpleNamespace(_modules=retained)
    with pytest.raises(TimeoutError):
        inventory._retained_modules(ledger, deadline=0)
    checks = []
    def check(deadline):
        checks.append(deadline)
        if len(checks) == 1:
            retained[('three', 3)] = (None, None)
    monkeypatch.setattr(ca, '_check_inventory_deadline', check)
    with pytest.raises(ValueError, match='^module_metadata_unavailable$'):
        inventory._retained_modules(ledger, deadline=None)


def test_namespace_mutation_is_fixed_refusal(monkeypatch):
    inventory = importlib.import_module('hermes_cli.code_attestation_inventory')
    from hermes_cli import code_attestation as ca
    module = ModuleType('ordinary')
    checks = []
    def check(deadline):
        checks.append(deadline)
        if len(checks) == 3:
            module.extra = 1
    monkeypatch.setattr(ca, '_check_inventory_deadline', check)
    with pytest.raises(ValueError, match='^module_metadata_unavailable$'):
        inventory._module_filename(module, deadline=None, budget=[0])
