"""Strict host collectors and canonical classification; no producer activation."""
import json
import os
import socket
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import serve_producer_admission as admission
from hermes_cli.serve_producer_census import Incarnation, LinuxProcessCensus, ProcessCensus, ProcessObservation

pytestmark = pytest.mark.platforms('linux')


def _home(tmp_path):
    root = tmp_path / 'hermes'
    root.mkdir()
    (root / 'spawn-ledger.json').write_text('[]')
    os.chmod(root / 'spawn-ledger.json', 0o600)
    return root


def test_missing_or_corrupt_ledger_is_not_empty_success(tmp_path):
    root = tmp_path / 'hermes'
    root.mkdir()
    with pytest.raises(admission.CensusUnavailable, match='missing'):
        admission.collect_raw_ledger(root)
    (root / 'spawn-ledger.json').write_text('{')
    os.chmod(root / 'spawn-ledger.json', 0o600)
    with pytest.raises(admission.CensusUnavailable):
        admission.collect_raw_ledger(root)


def test_permission_refusal_on_ledger_is_not_absence(tmp_path):
    root = _home(tmp_path)
    os.chmod(root / 'spawn-ledger.json', 0)
    try:
        with pytest.raises(admission.CensusUnavailable):
            admission.collect_raw_ledger(root)
    finally:
        os.chmod(root / 'spawn-ledger.json', 0o600)


def test_explicit_empty_ledger_is_positive_evidence_not_host_absence(tmp_path):
    evidence = admission.collect_raw_ledger(_home(tmp_path))
    assert evidence.refs and evidence.conflicts == ()


def test_socket_without_live_pid_is_unresolved_owner(tmp_path, monkeypatch):
    root = Path('/tmp') / f'sp{os.getpid()}'
    root.mkdir()
    try:
        (root / 'spawn-ledger.json').write_text('[]')
        os.chmod(root / 'spawn-ledger.json', 0o600)
        monkeypatch.setenv('HERMES_GATEWAY_LOCK_DIR', str(tmp_path / 'no-locks'))
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.bind(str(root / 'gateway.sock'))
            evidence = admission.collect_supervisor_ports(root)
        finally:
            sock.close()
        assert any('unresolved_owner' in item for item in evidence.conflicts)
    finally:
        for child in root.rglob('*'):
            if child.is_socket() or child.is_file():
                child.unlink(missing_ok=True)
        root.rmdir()


def test_unrelated_requires_path_free_identity_not_missing_enrollment():
    adapter = LinuxProcessCensus()
    current = adapter.read_process(os.getpid(), adapter.boot_id())
    foreign = replace(current, exe='/usr/bin/sleep', cwd='/tmp', argv=('/usr/bin/sleep', '1'), environment=())
    assert admission.classify_process(foreign, (), 'generation', installation='/opt/hermes').kind == 'unrelated'


def test_touching_python_without_enrollment_stays_unknown():
    adapter = LinuxProcessCensus()
    current = adapter.read_process(os.getpid(), adapter.boot_id())
    installation = str(Path(__file__).parents[2])
    assert admission.classify_process(current, (), 'generation', installation=installation).kind == 'unknown'


def test_known_launcher_module_without_enrollment_is_legacy():
    adapter = LinuxProcessCensus()
    current = adapter.read_process(os.getpid(), adapter.boot_id())
    installation = str(Path(__file__).parents[2])
    launched = replace(current, argv=(current.exe, '-m', 'hermes_cli.main', 'serve'))
    assert admission.classify_process(launched, (), 'generation', installation=installation).kind == 'legacy'


def test_real_collectors_can_cut_complete_only_with_unrelated_census(tmp_path, monkeypatch):
    root = _home(tmp_path)
    monkeypatch.setenv('HERMES_GATEWAY_LOCK_DIR', str(tmp_path / 'locks-missing'))
    installation = Path(__file__).parents[2]
    identity = Incarnation('boot', 9, 1)
    census = ProcessCensus('boot', 1, 2, (ProcessObservation(
        identity, '/usr/bin/sleep', '/tmp', ('/usr/bin/sleep',), ()),), (), ())
    with admission.LaunchGate.acquire(tmp_path / 'runtime') as gate:
        cut = gate.inspect(
            boot_id='boot', generation='generation', installation=str(installation),
            census=lambda: census, collectors=admission.host_collectors(root, installation),
            classify=lambda record: admission.Classification(record.identity, 'unrelated', 'exe'),
            legacy_candidates=(), exited=lambda identity: False)
        assert cut.complete
        gate.require(cut, boot_id='boot', generation='generation', installation=str(installation))


def test_launcher_coverage_is_complete_for_supported_sources():
    coverage = admission.launcher_coverage(Path(__file__).parents[2])
    assert coverage['status'] == 'COMPLETE'
    assert set(coverage['sources']) == set(admission.LAUNCH_SOURCES)
    assert set(coverage['roles']) == set(admission.SUPPORTED_LAUNCHES)


@pytest.mark.allow_real_home_io  # subject is the LIVE host census: classifying foreign processes lstat()s their
# PYTHONPATH entries (read-only metadata), which on a developer host may lie under the real Hermes home.
def test_inspect_host_hub_shaped_does_not_treat_live_python_as_empty(tmp_path, monkeypatch):
    root = _home(tmp_path)
    monkeypatch.setenv('HERMES_GATEWAY_LOCK_DIR', str(tmp_path / 'no-locks'))
    installation = Path(__file__).parents[2]
    cut = admission.inspect_host(tmp_path / 'runtime', installation, root=root)
    kinds = {row.kind for row in cut.classifications}
    assert 'unknown' in kinds or 'legacy' in kinds
    assert not cut.complete
    assert cut.census is not None and cut.census.processes
