"""Capsule consumers observe the installed provenance ledger, never construct one."""
from pathlib import Path

import pytest

from hermes_cli import code_attestation as attestation
from tests.hermes_cli.test_code_attestation import run_probe


def test_missing_installation_refuses_without_constructing(monkeypatch):
    monkeypatch.setattr(attestation, '_installed', None)
    with pytest.raises(RuntimeError, match='code attestation not installed'):
        attestation.integrity_observation()
    assert attestation._installed is None


def test_observation_preserves_pending_provenance_and_sticky_unknowns(tmp_path):
    result = run_probe(tmp_path, '''
        from hermes_cli import code_attestation as ca
        ca._installed = a
        first = ca.integrity_observation()
        a.mark_unknown('controlled-loaded-code-mismatch')
        second = ca.integrity_observation()
        print(json.dumps({'first': first, 'second': second, 'root': str(root),
                          'sha': subprocess_sha}))
    ''')
    first, second = result['first'], result['second']
    assert first['core_root'] == str(Path(result['root']).resolve())
    assert first['boot_sha'] == result['sha']
    assert first['route'] is None
    assert 'bootstrap-provenance-pending' in first['unknowns']
    assert 'controlled-loaded-code-mismatch' in second['unknowns']
    assert second['generation'] > first['generation']
