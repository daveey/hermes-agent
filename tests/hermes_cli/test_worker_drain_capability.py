"""Kernel-sealed daemon capability lifetime and identity controls."""
from __future__ import annotations

from pathlib import Path
import os

import pytest

from hermes_cli import worker_drain_capability as cap
from hermes_cli import worker_drain_census as census
from hermes_cli import worker_drain_fence as fence
from hermes_cli.worker_drain_baseline import CaptureRefused

pytestmark = pytest.mark.platforms('linux')


@pytest.fixture
def capability(monkeypatch, tmp_path):
    entry = census.process_identity('/proc', str(os.getpid()))
    ident = fence.Identity('i-12345678', 'boot-a', 'epoch-a', os.getuid())
    contract = {'digest': 'core-digest', 'installed_boot_id': 'boot-a', 'installed_start_ticks': entry[1] - 1,
                'activation_start_ticks': entry[1] - 1, 'homes': [str(tmp_path)],
                'core_root': str(Path(cap.__file__).resolve().parents[1])}
    monkeypatch.setattr(cap, 'verified_contract', lambda manifest: contract)
    monkeypatch.setattr(cap, 'startup_is_cold', lambda: True)
    fd = cap.create_capability(ident, {}, entry=entry)
    from types import SimpleNamespace
    from hermes_cli import worker_drain_ingress as ingress
    monkeypatch.setattr(ingress, '_inventory_fd', None)
    monkeypatch.setattr(ingress, '_inventory_revoked', False)
    runner = SimpleNamespace(config=SimpleNamespace(platforms={}), adapters={},
                             _profile_configs={}, _profile_adapters={})
    assert ingress.attest_runner(runner, contract['homes'])
    try:
        yield ident, entry, fd, contract
    finally:
        os.close(fd)
        os.close(ingress._inventory_fd)
        ingress._inventory_fd = None


def test_live_kernel_capability_matches_exact_process(capability):
    ident, entry, _, _ = capability
    cap.validate_capability(entry[0], entry, ident, {})


def test_cold_receipt_without_running_ingress_inventory_is_unknown(capability):
    from hermes_cli import worker_drain_ingress as ingress
    ident, entry, _, _ = capability
    fd = ingress._inventory_fd
    os.close(fd)
    assert os.open('/dev/null', os.O_RDONLY) == fd
    with pytest.raises(CaptureRefused):
        cap.validate_capability(entry[0], entry, ident, {})


def test_capability_cannot_be_rewritten_or_inherited_through_exec(capability):
    _, _, fd, _ = capability
    assert not os.get_inheritable(fd)
    with pytest.raises(OSError):
        os.write(fd, b'forged')


@pytest.mark.parametrize('field,value', [('digest', 'new-digest'), ('installed_boot_id', 'old-boot'),
    ('installed_start_ticks', 10**20), ('activation_start_ticks', 10**20), ('homes', ['/other/home'])])
def test_changed_runtime_contract_is_unknown(capability, field, value):
    ident, entry, _, contract = capability
    contract[field] = value
    with pytest.raises(CaptureRefused):
        cap.validate_capability(entry[0], entry, ident, {})


@pytest.mark.parametrize('ident', [fence.Identity('i-87654321', 'boot-a', 'epoch-a'),
    fence.Identity('i-12345678', 'boot-b', 'epoch-a'), fence.Identity('i-12345678', 'boot-a', 'epoch-b')])
def test_other_boot_instance_or_epoch_has_no_capability(capability, ident):
    _, entry, _, _ = capability
    with pytest.raises(CaptureRefused):
        cap.validate_capability(entry[0], entry, ident, {})


def test_capability_disappears_with_descriptor(capability):
    ident, entry, fd, _ = capability
    os.close(fd)
    # Keep the fixture's own descriptor valid for final cleanup, without retaining
    # the capability inode in this process.
    replacement = os.open('/dev/null', os.O_RDONLY)
    assert replacement == fd
    with pytest.raises(CaptureRefused):
        cap.validate_capability(entry[0], entry, ident, {})


def test_mint_cannot_attest_a_different_installed_tree(capability):
    ident, entry, _, contract = capability
    contract['core_root'] = '/other/reviewed/core'
    with pytest.raises(CaptureRefused, match='running core'):
        cap.create_capability(ident, {}, entry=entry)


def test_pre_fence_start_cannot_mint_even_without_ingress(capability):
    ident, entry, _, contract = capability
    contract['installed_start_ticks'] = entry[1]
    with pytest.raises(CaptureRefused, match='predates'):
        cap.create_capability(ident, {}, entry=entry)


def test_runtime_contract_requires_every_bounded_coverage_file(tmp_path):
    import hashlib
    files = {}
    for name in cap.REQUIRED_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'reviewed')
        files[name] = hashlib.sha256(b'reviewed').hexdigest()
    contract = {'core_root': str(tmp_path), 'files': files, 'capabilities': list(cap.CAPABILITIES),
                'homes': [str(tmp_path)], 'installed_start_ticks': 1, 'activation_start_ticks': 1}
    assert cap.verified_contract({'runtime': contract})['digest']
    for name in tuple(files):
        digest = files.pop(name)
        with pytest.raises(CaptureRefused):
            cap.verified_contract({'runtime': contract})
        files[name] = digest
    (tmp_path / 'hermes_cli/worker_drain_ingress.py').write_bytes(b'changed')
    with pytest.raises(CaptureRefused):
        cap.verified_contract({'runtime': contract})


def test_warm_daemon_cannot_gain_capability(capability, monkeypatch):
    ident, entry, _, _ = capability
    monkeypatch.setattr(cap, 'startup_is_cold', lambda: False)
    with pytest.raises(CaptureRefused, match='startup'):
        cap.create_capability(ident, {}, entry=entry)
