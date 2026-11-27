"""Requested inventory emits bounded stage totals, never module paths or reasons."""
from types import ModuleType
import logging
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from hermes_cli import code_attestation as ca
from hermes_cli import code_inventory_diagnostics as diagnostics


def test_cold_attestation_import_loads_observer_before_ledger_activation(tmp_path):
    source = '''
import sys
sys.path.insert(0, sys.argv[1])
from hermes_cli import code_attestation as ca
observer = sys.modules.get('hermes_cli.code_inventory_diagnostics')
assert observer is not None, 'observer must load before any ledger can activate'
assert ca.inventory_modules is observer.inventory_modules
assert 'logging' not in sys.modules, 'observer must not initialize logging'
'''
    result = subprocess.run(
        [sys.executable, '-I', '-S', '-B', '-c', source, str(Path(ca.__file__).parents[1])],
        cwd=tmp_path, capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_actual_inventory_reports_stages_without_changing_module_observation(tmp_path, caplog):
    ledger = object.__new__(ca.Attestation)
    ledger._components = {}
    ledger._stdlib = tmp_path
    ledger._modules = {}
    ledger._generation = 0
    module = ModuleType('not-secret-in-log')
    module.__file__ = str(tmp_path / 'not-secret-in-log.py')
    ledger._module_snapshot = lambda: (('not-secret-in-log', module),)
    with caplog.at_level(logging.INFO, logger='hermes_cli.code_inventory_diagnostics'):
        ledger._lock = threading.RLock()
        with diagnostics.InventoryObservation(ledger) as timer:
            ledger._inventory_modules(diagnostics=timer)
    assert ledger._modules == {('not-secret-in-log', id(module)): (module, module.__file__)}
    assert ledger._generation == 1
    rows = [r for r in caplog.records if r.name == 'hermes_cli.code_inventory_diagnostics']
    assert len(rows) == 1
    assert 'not-secret-in-log' not in rows[0].getMessage()
    assert set(rows[0].args[-1]) == {'resolve', 'owner', 'classify', 'modules'}


def test_actual_operations_have_distinct_wall_cpu_totals(tmp_path, monkeypatch, caplog):
    ledger = object.__new__(ca.Attestation)
    ledger._components = {}
    ledger._stdlib = tmp_path
    ledger._modules = {}
    ledger._generation = 0
    module = ModuleType('observed')
    module.__file__ = str(tmp_path / 'observed.py')
    monkeypatch.setattr(ledger, '_module_snapshot', lambda: (('observed', module),))
    clocks = [10_000_000_000, 1_000_000_000]
    monkeypatch.setattr(diagnostics, 'monotonic_ns', lambda: clocks[0])
    monkeypatch.setattr(diagnostics, 'thread_time_ns', lambda: clocks[1])
    for target, name, wall, cpu in ((ca.Path, 'resolve', 3, .1),
                                   (ledger, '_owner_path', 4, .2),
                                   (ledger, '_classify_module_file', 5, .3)):
        original = getattr(target, name)

        def delayed(*args, _original=original, _wall=wall, _cpu=cpu, **kwargs):
            value = _original(*args, **kwargs)
            clocks[0] += int(_wall * 1_000_000_000)
            clocks[1] += int(_cpu * 1_000_000_000)
            return value

        monkeypatch.setattr(target, name, delayed)
    with caplog.at_level(logging.INFO, logger=diagnostics.__name__):
        ledger._lock = threading.RLock()
        with diagnostics.InventoryObservation(ledger) as timer:
            ledger._inventory_modules(diagnostics=timer)
    row = next(r for r in caplog.records if r.name == diagnostics.__name__)
    assert row.args[2:6] == (10_000_000_000, 1, 12000.0, 600.0)
    assert row.args[-1] == {'resolve': (1, 3000.0, 100.0), 'owner': (1, 4000.0, 200.0),
                            'classify': (1, 5000.0, 300.0), 'modules': (1, 0.0, 0.0)}


@pytest.mark.parametrize('operation_raises', [False, True])
@pytest.mark.parametrize('failure', ['enabled', 'begin_wall', 'begin_cpu', 'end_wall', 'sink'])
def test_optional_observer_failures_never_replace_operation(monkeypatch, operation_raises, failure):
    original = ValueError('original operation failed')
    calls = []

    def operation():
        calls.append('called')
        if operation_raises:
            raise original
        return 'unchanged'

    def broken(*_args):
        raise OSError('optional observer unavailable')

    monkeypatch.setattr(diagnostics.logger, 'isEnabledFor', lambda _level: True)
    if failure == 'enabled':
        monkeypatch.setattr(diagnostics.logger, 'isEnabledFor', broken)
    elif failure == 'begin_wall':
        monkeypatch.setattr(diagnostics, 'monotonic_ns', broken)
    elif failure == 'begin_cpu':
        monkeypatch.setattr(diagnostics, 'thread_time_ns', broken)
    timer = diagnostics.InventoryDiagnostics()
    if failure == 'end_wall':
        readings = iter([10])

        def end_fails():
            try:
                return next(readings)
            except StopIteration:
                raise OSError('optional clock failed') from None

        monkeypatch.setattr(diagnostics, 'monotonic_ns', end_fails)
    if failure == 'sink':
        monkeypatch.setattr(diagnostics.logger, 'info', broken)
    if operation_raises:
        with pytest.raises(ValueError) as raised:
            timer.call('resolve', operation)
        assert raised.value is original
    else:
        assert timer.call('resolve', operation) == 'unchanged'
    timer.finish()
    assert calls == ['called']


def test_disabled_observation_does_not_read_clocks(monkeypatch):
    monkeypatch.setattr(diagnostics.logger, 'isEnabledFor', lambda _level: False)

    def forbidden():
        raise AssertionError('disabled diagnostics read a clock')

    monkeypatch.setattr(diagnostics, 'monotonic_ns', forbidden)
    monkeypatch.setattr(diagnostics, 'thread_time_ns', forbidden)
    timer = diagnostics.InventoryDiagnostics()
    assert timer.call('resolve', lambda: 7) == 7
    timer.finish()


def test_fresh_resolve_detects_retargeted_symlink_for_unchanged_module(tmp_path, monkeypatch):
    ledger = object.__new__(ca.Attestation)
    trusted = tmp_path / 'trusted'
    foreign = tmp_path / 'foreign'
    trusted.mkdir()
    foreign.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(trusted, target_is_directory=True)
    ledger._components = {}
    ledger._stdlib = trusted
    ledger._modules = {}
    ledger._generation = 0
    module = ModuleType('observed')
    module.__file__ = str(alias / 'module.py')
    monkeypatch.setattr(ledger, '_module_snapshot', lambda: (('observed', module),))
    monkeypatch.setattr(ledger, '_dependency_module_path', lambda *_args: False)
    unknowns = []
    monkeypatch.setattr(ledger, '_extension_unknown', lambda *args: unknowns.append(args))
    ledger._inventory_modules()
    assert unknowns == []
    alias.unlink()
    alias.symlink_to(foreign, target_is_directory=True)
    ledger._inventory_modules()
    assert unknowns == [('unrooted-module:observed', module.__file__)]
    assert ledger._generation == 1  # no identity change, but fresh classification still ran


def test_late_initialized_sink_is_found_without_importing_logging(monkeypatch):
    monkeypatch.setattr(diagnostics, 'logger', None)
    fake_sys = ModuleType('inventory_sys')
    fake_sys.modules = {}
    monkeypatch.setattr(diagnostics, 'sys', fake_sys)
    assert diagnostics.InventoryDiagnostics().enabled is False
    calls = []
    sink = ModuleType('sink')
    sink.isEnabledFor = lambda _level: True
    sink.info = lambda *args: calls.append(args)
    ready = ModuleType('logging')
    ready.getLogger = lambda _name: sink
    fake_sys.modules['logging'] = ready
    timer = diagnostics.InventoryDiagnostics()
    assert timer.enabled is True
    assert timer.call('resolve', lambda: 7) == 7
    timer.finish()
    assert len(calls) == 1
