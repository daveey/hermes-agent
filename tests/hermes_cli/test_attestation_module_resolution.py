"""Fresh per-module path custody without repeatedly traversing the same path."""
import threading
from pathlib import Path
from types import SimpleNamespace

from hermes_cli import code_attestation as ca


def ledger(tmp_path):
    a = object.__new__(ca.Attestation)
    a._lock = threading.RLock()
    a._unknown = set()
    a._blocking_unknowns = set()
    a._extension_unknowns = set()
    a._generation = 0
    a._modules = {}
    a._dep_units = {}
    a.core_root = tmp_path / 'core'
    a._components = {'core': (a.core_root, 'core', {})}
    a._stdlib = tmp_path / 'stdlib'
    a._deps = ca._DependencyIndex(tmp_path, [])
    return a


def test_module_classification_resolves_filename_once(tmp_path, monkeypatch):
    a = ledger(tmp_path)
    path = tmp_path / 'outside' / 'module.py'
    module = SimpleNamespace(__file__=str(path))
    monkeypatch.setattr(a, '_module_snapshot', lambda: [('fixture', module)])
    original = Path.resolve
    calls = []

    def resolve(self, *args, **kwargs):
        calls.append(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', resolve)
    a._inventory_modules()
    # One classification resolution plus the independent extension-emission check.
    assert calls.count(str(path)) == 2
    assert a.latched_unknowns() == ('unrooted-module:fixture',)


def test_inventory_rechecks_symlink_even_for_same_module(tmp_path, monkeypatch):
    a = ledger(tmp_path)
    a.core_root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = tmp_path / 'module.py'
    link.symlink_to(a.core_root / 'module.py')
    module = SimpleNamespace(__file__=str(link))
    monkeypatch.setattr(a, '_module_snapshot', lambda: [('fixture', module)])
    a._inventory_modules()
    assert a.latched_unknowns() == ()
    link.unlink()
    link.symlink_to(outside / 'module.py')
    a._inventory_modules()
    assert a.latched_unknowns() == ('unrooted-module:fixture',)


def test_inventory_rechecks_changed_module_filename(tmp_path, monkeypatch):
    a = ledger(tmp_path)
    module = SimpleNamespace(__file__=str(a.core_root / 'module.py'))
    monkeypatch.setattr(a, '_module_snapshot', lambda: [('fixture', module)])
    a._inventory_modules()
    assert a.latched_unknowns() == ()
    module.__file__ = str(tmp_path / 'outside' / 'module.py')
    a._inventory_modules()
    assert a.latched_unknowns() == ('unrooted-module:fixture',)


def test_report_retains_measured_mac_dependency_cardinality(tmp_path):
    a = ledger(tmp_path)
    # Read-only Mac RECORD inventory: 23,655 sha256-bearing Python source rows.
    # The live ledger already filled 4096 with 4009 source / 87 other units.
    measured_sources = 23655
    for index in range(measured_sources):
        a._dep_unit(f'/receipted/source{index}.py', 'dependency:fixture-1', 'a' * 64, 'source')
    assert len(a._dep_units) == measured_sources
    assert 'dependency-report-limit' not in a.latched_unknowns()


def test_report_overflow_remains_bounded_and_sticky(tmp_path, monkeypatch):
    a = ledger(tmp_path)
    monkeypatch.setattr(ca, 'MAX_DEP_UNITS', 2)
    for index in range(3):
        a._dep_unit(f'/receipted/source{index}.py', 'dependency:fixture-1', 'a' * 64, 'source')
    assert len(a._dep_units) == 2
    assert a.latched_unknowns() == ('dependency-report-limit',)
    a._dep_units.clear()
    assert a.latched_unknowns() == ('dependency-report-limit',)
