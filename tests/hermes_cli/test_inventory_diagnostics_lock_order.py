"""Observer logging must stay outside the public ledger lock boundary."""
import logging
import os
import threading
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from hermes_cli import code_attestation as ca
from hermes_cli import code_inventory_diagnostics as diagnostics


def ledger_fixture(tmp_path):
    ledger = object.__new__(ca.Attestation)
    ledger._lock = threading.RLock()
    ledger._tls = threading.local()
    ledger._pid = os.getpid()
    ledger._components = {}
    ledger._stdlib = tmp_path
    ledger._modules = {}
    ledger._generation = 0
    ledger._unknown = set()
    ledger._extension_unknowns = set()
    ledger._blocking_unknowns = set()
    ledger.core_root = tmp_path
    ledger.boot_sha = 'fixture'
    ledger.route = None
    ledger._module_snapshot = lambda: ()
    ledger._settle = lambda **_kwargs: None
    return ledger


@pytest.mark.parametrize('entry', ['integrity_observation', 'unknowns', 'partition'])
def test_sink_lookup_enablement_and_emit_are_outside_ledger_lock(tmp_path, monkeypatch, entry):
    ledger = ledger_fixture(tmp_path)
    events = []

    class Sink:
        def isEnabledFor(self, _level):
            assert not ledger._lock._is_owned(), 'enablement under ledger lock'
            events.append('enabled')
            return True

        def info(self, *_args):
            assert not ledger._lock._is_owned(), 'emit under ledger lock'
            events.append('emit')

    def getter(_name):
        assert not ledger._lock._is_owned(), 'lookup under ledger lock'
        events.append('lookup')
        return Sink()

    monkeypatch.setattr(diagnostics, 'logger', None)
    fake_logging = ModuleType('logging')
    fake_logging.getLogger = getter
    fake_sys = ModuleType('observer_sys')
    fake_sys.modules = {'logging': fake_logging}
    monkeypatch.setattr(diagnostics, 'sys', fake_sys)
    if entry == 'partition':
        ledger.quiet_unknown_partition(inventory=True)
    else:
        getattr(ledger, entry)()
    assert events == ['lookup', 'enabled', 'emit']


def test_nested_public_read_never_logs_under_outer_ledger_lock(tmp_path, monkeypatch):
    ledger = ledger_fixture(tmp_path)

    def forbidden(*_args):
        raise AssertionError('nested reader consulted logging')

    monkeypatch.setattr(diagnostics, 'InventoryDiagnostics', forbidden)
    with ledger._lock:
        assert ledger.unknowns() == ()
        assert ledger.integrity_observation()['unknowns'] == ()
        assert ledger.quiet_unknown_partition(inventory=True) == ([], [])


def test_completed_row_excludes_time_after_scan_and_before_emit(tmp_path, monkeypatch, caplog):
    ledger = ledger_fixture(tmp_path)
    clocks = [100, 10]
    monkeypatch.setattr(diagnostics, 'monotonic_ns', lambda: clocks[0])
    monkeypatch.setattr(diagnostics, 'thread_time_ns', lambda: clocks[1])
    with caplog.at_level(logging.INFO, logger=diagnostics.__name__):
        with diagnostics.InventoryObservation(ledger) as timer:
            ledger._inventory_modules(diagnostics=timer)
            clocks[0] += 20_000_000
            clocks[1] += 10_000_000
    row = next(r for r in caplog.records if r.name == diagnostics.__name__)
    assert 'outcome=completed' in row.getMessage()
    assert row.args[4:6] == (0.0, 0.0)


def test_no_inventory_partition_does_not_consult_logging(tmp_path, monkeypatch):
    ledger = ledger_fixture(tmp_path)

    def forbidden(*_args):
        raise AssertionError('cheap partition consulted logging')

    monkeypatch.setattr(diagnostics, 'InventoryDiagnostics', forbidden)
    assert ledger.quiet_unknown_partition() == ([], [])


def test_deadline_aborted_inventory_emits_one_partial_row_and_preserves_exception(tmp_path, monkeypatch, caplog):
    ledger = ledger_fixture(tmp_path)
    module = ModuleType('first')
    ledger._module_snapshot = lambda: (('first', module), ('second', module))
    original = TimeoutError('original inventory deadline')
    checks = []

    def check(_deadline):
        checks.append(None)
        # Public entry, after settlement, first module, then second module.
        if len(checks) == 4:
            raise original

    monkeypatch.setattr(ca, '_check_inventory_deadline', check)
    with caplog.at_level(logging.INFO, logger=diagnostics.__name__):
        with pytest.raises(TimeoutError) as raised:
            ledger.integrity_observation(deadline=time.monotonic() + 60)
    assert raised.value is original
    rows = [r for r in caplog.records if r.name == diagnostics.__name__]
    assert len(rows) == 1
    assert 'outcome=aborted' in rows[0].getMessage()
    assert 'modules=1' in rows[0].getMessage()


@pytest.mark.parametrize('mode', ['module_lock', 'handler_lock'])
def test_logging_holder_can_enter_real_audit_during_inventory(tmp_path, monkeypatch, mode):
    if os.environ.get('HERMES_INVENTORY_LOCK_PROBE') != mode:
        result = subprocess.run(
            [sys.executable, '-m', 'pytest', '-q',
             str(Path(__file__).resolve()) + '::test_logging_holder_can_enter_real_audit_during_inventory[' + mode + ']'],
            env={**os.environ, 'HERMES_INVENTORY_LOCK_PROBE': mode},
            capture_output=True, text=True, timeout=25, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        return
    ledger = ledger_fixture(tmp_path)
    holding_log = threading.Event()
    in_inventory = threading.Event()
    audit_entered = threading.Event()
    done = threading.Event()
    errors = []
    handler = logging.NullHandler()
    log_lock = logging._lock if mode == 'module_lock' else handler.lock
    if log_lock is None:
        handler.createLock()
        # NullHandler deliberately has no lock; use the ordinary handler lock.
        handler = logging.Handler()
        log_lock = handler.lock

    def settle(**_kwargs):
        in_inventory.set()
        assert holding_log.wait(5)
        assert audit_entered.wait(5)

    ledger._settle = settle
    monkeypatch.setattr(diagnostics, 'logger', None if mode == 'module_lock' else diagnostics.logger)
    sink = diagnostics.logger
    if mode == 'handler_lock':
        monkeypatch.setattr(sink, 'isEnabledFor', lambda _level: True)
        monkeypatch.setattr(sink, 'info', lambda *_args: handler.acquire() or handler.release())

    def holder():
        try:
            with log_lock:
                holding_log.set()
                assert in_inventory.wait(5)
                audit_entered.set()
                ledger._audit('import', ('inventory_sentinel', None))
            done.set()
        except BaseException as exc:
            errors.append(exc)

    # Run public observation first: logging enablement may legitimately wait for
    # another logging holder, but must not own the ledger during that wait.
    def observe():
        try:
            ledger.integrity_observation()
        except BaseException as exc:
            errors.append(exc)

    a = threading.Thread(target=observe, daemon=True)
    b = threading.Thread(target=holder, daemon=True)
    a.start()
    assert in_inventory.wait(5)
    b.start()
    a.join(6)
    b.join(6)
    assert not a.is_alive() and not b.is_alive(), 'logging/ledger inversion deadlocked'
    assert done.is_set()
    assert errors == []
