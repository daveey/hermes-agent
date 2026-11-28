"""Prepared inventory failures retain honest off-lock timing, never success."""
import logging

import pytest

from hermes_cli import code_attestation as ca
from hermes_cli import code_inventory_diagnostics as diagnostics
from tests.hermes_cli.test_inventory_diagnostics_lock_order import ledger_fixture


@pytest.mark.parametrize(('entry', 'failure'), [
    (entry, failure)
    for failure in ('settle', 'reacquire')
    for entry in ('integrity_observation', 'unknowns', 'partition')
] + [('integrity_observation', 'deadline')])
def test_prepared_failure_is_aborted_and_excludes_settlement(
        tmp_path, monkeypatch, caplog, entry, failure):
    ledger = ledger_fixture(tmp_path)
    clocks = [100, 10]
    monkeypatch.setattr(diagnostics, 'monotonic_ns', lambda: clocks[0])
    monkeypatch.setattr(diagnostics, 'thread_time_ns', lambda: clocks[1])
    original = TimeoutError('prepared inventory failed')
    events = []

    def snapshot():
        events.append('prepare')
        clocks[0] += 4_000_000
        clocks[1] += 1_000_000
        return ()

    def settle(**_kwargs):
        if events:
            events.append('settle')
            clocks[0] += 9_000_000_000
            clocks[1] += 2_000_000_000
            if failure == 'settle':
                raise original

    def check(_deadline):
        if 'settle' in events and failure == 'deadline':
            raise original

    acquire = diagnostics.InventoryObservation._acquire

    def reacquire(observation):
        if events and failure == 'reacquire':
            events.append('reacquire')
            clocks[0] += 7_000_000_000
            clocks[1] += 1_000_000_000
            raise original
        return acquire(observation)

    ledger._module_snapshot = snapshot
    ledger._settle = settle
    monkeypatch.setattr(ca, '_check_inventory_deadline', check)
    monkeypatch.setattr(diagnostics.InventoryObservation, '_acquire', reacquire)
    with caplog.at_level(logging.INFO, logger=diagnostics.__name__):
        with pytest.raises(TimeoutError) as raised:
            if entry == 'partition':
                ledger.quiet_unknown_partition(inventory=True)
            else:
                getattr(ledger, entry)()
    assert raised.value is original
    assert not ledger._lock._is_owned()
    assert events == ['prepare', 'reacquire' if failure == 'reacquire' else 'settle']
    rows = [r for r in caplog.records if r.name == diagnostics.__name__]
    assert len(rows) == 1
    assert rows[0].args[6] == 'aborted'
    assert rows[0].args[4:6] == (4.0, 1.0)
    assert rows[0].args[-1]['modules'] == (0, 4.0, 1.0)
