"""An expired sibling import must not enter the ledger-lock phase."""
from types import SimpleNamespace

import pytest

from hermes_cli import code_attestation_snapshot as snapshots


def test_expiry_during_sibling_import_refuses_before_lock_attempt(monkeypatch, tmp_path):
    clock, phases, acquired = [0.0], [], []

    def import_sibling(name):
        assert name == 'hermes_cli.code_attestation_inventory'
        clock[0] = 2.0

    def acquire(**kwargs):
        acquired.append(kwargs)
        return False

    monkeypatch.setattr(snapshots.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(snapshots, 'disk_identity', lambda root, deadline: None)
    monkeypatch.setattr(snapshots.importlib, 'import_module', import_sibling)
    ledger = SimpleNamespace(core_root=tmp_path, _lock=SimpleNamespace(acquire=acquire))
    progress = SimpleNamespace(set_phase=phases.append)
    with pytest.raises(TimeoutError, match='^snapshot_deadline$'):
        snapshots.snapshot(ledger, deadline=2.0, progress=progress)
    assert phases == ['disk']
    assert acquired == []
