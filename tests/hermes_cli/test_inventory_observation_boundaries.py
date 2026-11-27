"""Scan attribution, updater closure and optional-observer contention boundaries."""
import logging
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

from hermes_cli import code_attestation as ca
from hermes_cli import code_inventory_diagnostics as diagnostics
from hermes_cli import update_cmd_commit
from tests.hermes_cli.test_inventory_diagnostics_lock_order import ledger_fixture


def test_extracted_observer_is_declared_and_checked_by_updater(tmp_path):
    root = Path(ca.__file__).resolve().parents[1]
    declared, required = update_cmd_commit.startup_files((root / 'pyproject.toml').read_bytes(), ())
    relative = 'hermes_cli/code_inventory_diagnostics.py'
    assert required and relative in declared
    (tmp_path / 'pyproject.toml').write_bytes((root / 'pyproject.toml').read_bytes())
    for name in declared:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('ok = 1\n')
    assert update_cmd_commit.tree_syntax_error(tmp_path, ()) is None
    (tmp_path / relative).write_text('def broken(:\n')
    refused = update_cmd_commit.tree_syntax_error(tmp_path, ())
    assert refused is not None and refused[0] == relative


def test_inventory_partition_settles_pending_evidence_before_scan_and_read(tmp_path):
    ledger = ledger_fixture(tmp_path)
    events = []

    def settle():
        assert ledger._lock._is_owned()
        events.append('settle')
        ledger.mark_unknown('pending-source-skew')

    def scan(**_kwargs):
        assert events == ['settle']
        assert ledger._lock._is_owned()
        events.append('scan')

    ledger._settle = settle
    ledger._inventory_modules = scan
    assert ledger.quiet_unknown_partition(inventory=True) == (['pending-source-skew'], [])
    assert events == ['settle', 'scan']


@pytest.mark.parametrize('entry', ['integrity_observation', 'unknowns', 'partition'])
def test_scan_clock_excludes_settlement_and_lock_acquisition(tmp_path, monkeypatch, caplog, entry):
    ledger = ledger_fixture(tmp_path)
    clocks = [100, 10]
    monkeypatch.setattr(diagnostics, 'monotonic_ns', lambda: clocks[0])
    monkeypatch.setattr(diagnostics, 'thread_time_ns', lambda: clocks[1])

    def settle(**_kwargs):
        clocks[0] += 9_000_000_000
        clocks[1] += 2_000_000_000

    def snapshot():
        clocks[0] += 4_000_000
        clocks[1] += 1_000_000
        return ()

    ledger._settle = settle
    ledger._module_snapshot = snapshot
    with caplog.at_level(logging.INFO, logger=diagnostics.__name__):
        if entry == 'partition':
            ledger.quiet_unknown_partition(inventory=True)
        else:
            getattr(ledger, entry)()
    rows = [r for r in caplog.records if r.name == diagnostics.__name__]
    assert len(rows) == 1
    assert rows[0].args[4:6] == (4.0, 1.0)
    assert rows[0].args[-1]['modules'] == (0, 4.0, 1.0)


@pytest.mark.parametrize('failure', ['settle', 'pre_scan_deadline'])
def test_reader_failure_before_scan_emits_no_inventory_row(tmp_path, monkeypatch, caplog, failure):
    ledger = ledger_fixture(tmp_path)
    original = TimeoutError('before inventory')
    checks = []

    def fail(**_kwargs):
        raise original

    def check(_deadline):
        checks.append(None)
        if len(checks) == 2:
            raise original

    if failure == 'settle':
        ledger._settle = fail
    else:
        monkeypatch.setattr(ca, '_check_inventory_deadline', check)
    with caplog.at_level(logging.INFO, logger=diagnostics.__name__):
        with pytest.raises(TimeoutError) as raised:
            ledger.integrity_observation()
    assert raised.value is original
    assert not [r for r in caplog.records if r.name == diagnostics.__name__]


def test_late_sink_is_cached_after_first_lookup(monkeypatch):
    calls = []
    sink = logging.getLogger(diagnostics.__name__)
    fake_logging = ModuleType('logging')
    fake_logging.getLogger = lambda name: calls.append(name) or sink
    fake_sys = ModuleType('observer_sys')
    fake_sys.modules = {'logging': fake_logging}
    monkeypatch.setattr(diagnostics, 'sys', fake_sys)
    monkeypatch.setattr(diagnostics, 'logger', None)
    diagnostics.InventoryDiagnostics()
    diagnostics.InventoryDiagnostics()
    assert calls == [diagnostics.__name__]


@pytest.mark.parametrize('cached_sink', [False, True])
def test_busy_logging_setup_does_not_delay_bounded_integrity_reader(tmp_path, monkeypatch, cached_sink):
    ledger = ledger_fixture(tmp_path)
    scans = []
    ledger._module_snapshot = lambda: scans.append('scanned') or ()
    held = threading.Event()
    release = threading.Event()
    done = threading.Event()
    results = []
    errors = []
    sink = logging.getLogger(diagnostics.__name__) if cached_sink else None
    monkeypatch.setattr(diagnostics, 'logger', sink)
    if sink is not None:
        monkeypatch.setattr(sink, '_cache', {})  # exercise isEnabledFor's module-lock miss too

    def holder():
        with logging._lock:
            held.set()
            release.wait(5)

    def reader():
        try:
            results.append(ledger.integrity_observation(deadline=time.monotonic() + 0.5))
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    a = threading.Thread(target=holder)
    b = threading.Thread(target=reader)
    a.start()
    try:
        assert held.wait(2)
        b.start()
        completed_while_held = done.wait(1)
    finally:
        release.set()
        a.join(3)
        if b.ident is not None:
            b.join(3)
    assert not a.is_alive() and not b.is_alive()
    assert completed_while_held, 'optional logging setup blocked the verified inventory'
    assert errors == []
    assert len(results) == 1 and results[0]['unknowns'] == ()
    assert scans == ['scanned']
