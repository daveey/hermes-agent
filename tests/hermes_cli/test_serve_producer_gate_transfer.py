"""B05: transfer actual locked descriptions, never a fresh gate or persisted readiness bit."""
import os
from pathlib import Path

import pytest

from hermes_cli import serve_producer_admission as admission
from hermes_cli.serve_producer_custody import CustodyConflict, CustodyUnsafe

pytestmark = pytest.mark.platforms('linux')
ROOT = Path(__file__).parents[2]


def test_adopt_duplicate_keeps_original_gate_locked_after_parent_close(tmp_path):
    runtime = tmp_path / 'runtime'
    parent = admission.LaunchGate.acquire(runtime)
    child = admission.LaunchGate.adopt(runtime, os.dup(parent.fd))
    try:
        assert not os.get_inheritable(child.fd)
        parent.close()
        with pytest.raises(CustodyConflict):
            admission.LaunchGate.acquire(runtime)
        # A borrowed gate does not get closed by enrollment and is never re-acquired.
        ticket = admission.enroll_launch(runtime, 'serve', installation=ROOT, gate=child)
        assert ticket in admission.read_participants_strict(runtime)
        child.require_runtime(runtime)
    finally:
        parent.close()
        child.close()
    admission.LaunchGate.acquire(runtime).close()


def test_same_inode_unlocked_description_is_not_inherited_custody(tmp_path):
    runtime = tmp_path / 'runtime'
    parent = admission.LaunchGate.acquire(runtime)
    fd = os.open(runtime / 'admission.lock', os.O_RDWR)
    try:
        with pytest.raises(admission.AdmissionUnavailable, match='not locked'):
            admission.LaunchGate.adopt(runtime, fd)
        with pytest.raises(OSError):
            os.fstat(fd)  # failed adoption consumes only the supplied description
        parent.require_runtime(runtime)
    finally:
        parent.close()


def test_unlocked_description_cannot_mint_a_gate(tmp_path):
    runtime = tmp_path / 'runtime'
    admission.LaunchGate.acquire(runtime).close()
    fd = os.open(runtime / 'admission.lock', os.O_RDWR)
    with pytest.raises(admission.AdmissionUnavailable, match='not locked'):
        admission.LaunchGate.adopt(runtime, fd)


def test_foreign_runtime_refuses_same_named_real_lock(tmp_path):
    left, right = tmp_path / 'left', tmp_path / 'right'
    first, second = admission.LaunchGate.acquire(left), admission.LaunchGate.acquire(right)
    try:
        with pytest.raises(admission.AdmissionUnavailable, match='inode changed'):
            admission.LaunchGate.adopt(right, os.dup(first.fd))
        with pytest.raises(admission.AdmissionUnavailable, match='inode changed'):
            admission.enroll_launch(right, 'serve', installation=ROOT, gate=first)
        assert not (right / 'participants.json').exists()
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize('target', ['directory', 'file'])
def test_unsafe_permissions_refuse_adoption(tmp_path, target):
    runtime = tmp_path / 'runtime'
    gate = admission.LaunchGate.acquire(runtime)
    path = runtime if target == 'directory' else runtime / 'admission.lock'
    try:
        path.chmod(0o755 if target == 'directory' else 0o644)
        with pytest.raises(CustodyUnsafe):
            admission.LaunchGate.adopt(runtime, os.dup(gate.fd))
    finally:
        path.chmod(0o700 if target == 'directory' else 0o600)
        gate.close()


def test_closed_and_foreign_process_gate_cannot_be_transferred(tmp_path, monkeypatch):
    gate = admission.LaunchGate.acquire(tmp_path / 'r')
    original_pid = gate._pid
    monkeypatch.setattr(gate, '_pid', original_pid + 100000)
    with pytest.raises(admission.AdmissionUnavailable, match='collecting process'):
        _ = gate.fd
    monkeypatch.setattr(gate, '_pid', original_pid)
    gate.close()
    with pytest.raises(admission.AdmissionUnavailable, match='collecting process'):
        _ = gate.fd


def test_shared_broker_without_gate_refuses_before_custody(tmp_path, monkeypatch):
    from hermes_cli import serve_producer_broker as shared
    from tests.hermes_cli import test_serve_producer_shared_broker as helpers

    messages = []
    spec = helpers.write_provider(tmp_path)
    class Startup:
        def send(self, message, sequence):
            messages.append(message)
        def close(self):
            messages.append('closed')
    shared.run_shared(Startup(), {'runtime': str(tmp_path / 'r'), 'provider': spec,
                                  'initial': {'ok': True}})
    assert messages == [{'verb': 'REFUSED', 'error': 'admission gate missing'}, 'closed']
    assert not (tmp_path / 'r').exists()
