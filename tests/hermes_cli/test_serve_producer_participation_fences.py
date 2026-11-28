"""Negative custody observations: descendants and stale witnesses fail closed."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import serve_producer_admission as admission
from hermes_cli import serve_producer_participation as participation
from hermes_cli.serve_producer_census import CensusUnavailable, LinuxProcessCensus
from hermes_cli.serve_producer_custody import CustodyConflict

pytestmark = pytest.mark.platforms('linux')
ROOT = Path(__file__).parents[2]


def setup_guard(tmp_path):
    runtime = tmp_path / 'runtime'
    ticket = admission.enroll_launch(runtime, 'serve', installation=ROOT)
    guard = participation.PanelParticipant(runtime, ticket)
    adapter = LinuxProcessCensus()
    record = adapter.read_process(os.getpid(), adapter.boot_id())
    def retired():
        return participation.retired_participant(record, (ticket,), runtime, ticket.generation, str(ROOT))
    return runtime, guard, retired


def test_failed_repeat_drain_revokes_prior_kernel_witness(tmp_path):
    _, guard, retired = setup_guard(tmp_path)
    try:
        assert guard.retire(lambda: True)
        assert not guard.retire(lambda: False)
        assert not retired()
    finally:
        guard.close()


@pytest.mark.parametrize('lane', ['family', 'execution'])
def test_surviving_descendant_keeps_fences_and_refuses_cleanup(tmp_path, lane):
    runtime, guard, retired = setup_guard(tmp_path)
    with admission.LaunchGate.acquire(runtime) as gate:
        guard.begin_legacy(gate)
    source = tmp_path / 'descendant.py'
    source.write_text("import sys\nprint('ready', flush=True)\nsys.stdin.buffer.read()\n")
    held = guard._owner if lane == 'family' else guard._execution
    assert held is not None and held.fd is not None
    child = subprocess.Popen([sys.executable, str(source)], pass_fds=(held.fd,),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == 'ready'
        with pytest.raises(CustodyConflict):
            guard.retire(lambda: True)
        assert not retired()
        with pytest.raises(admission.AdmissionUnavailable, match='still owns'):
            guard.close()
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=10)
        assert guard.retire(lambda: True)
        guard.close()


@pytest.mark.parametrize('damage', ['unlocked', 'permissions', 'descriptor'])
def test_witness_requires_private_pid_owned_locked_description(tmp_path, damage):
    runtime, guard, retired = setup_guard(tmp_path)
    try:
        assert guard.retire(lambda: True)
        path = runtime / (participation._name(guard.ticket) + '.lock')
        if damage == 'unlocked':
            assert guard._witness is not None
            guard._witness.close()
        elif damage == 'permissions':
            path.chmod(0o644)
        else:
            path.write_text('1000000')
        assert not retired()
    finally:
        guard.close()


def test_inaccessible_live_identity_is_not_retirement(tmp_path, monkeypatch):
    _, guard, retired = setup_guard(tmp_path)
    try:
        assert guard.retire(lambda: True)
        def unavailable(*args):
            raise CensusUnavailable('fixture observation inaccessible')
        monkeypatch.setattr(participation.LinuxProcessCensus, 'read_process', unavailable)
        assert not retired()
    finally:
        guard.close()
