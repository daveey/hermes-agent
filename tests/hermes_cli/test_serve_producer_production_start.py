"""Production start is LaunchGate COMPLETE, not the pytest fixture fence."""
import pytest

from hermes_cli.serve_producer_admission import (
    REQUIRED_COLLECTORS, AdmissionCut, AdmissionUnavailable, CollectorEvidence, LaunchGate,
)
from hermes_cli.serve_producer_runtime import ProjectionRelay


def _complete_cut(gate, installation):
    collectors = tuple((name, CollectorEvidence((f'{name}:ok',), ())) for name in sorted(REQUIRED_COLLECTORS))
    return AdmissionCut(
        boot_id='boot', generation='a' * 64, installation=str(installation),
        census=None, collectors=collectors, classifications=(),
        legacy_candidates=(), retired=(), errors=(), _acquisition=gate._acquisition)


def test_production_start_refuses_incomplete_cut(tmp_path):
    relay = ProjectionRelay({}, home=tmp_path, runtime=tmp_path / 'runtime', generation='g')
    with LaunchGate.acquire(tmp_path / 'runtime') as gate:
        cut = AdmissionCut(
            boot_id='boot', generation='a' * 64, installation=str(tmp_path),
            census=None, collectors=(), classifications=(),
            legacy_candidates=(), retired=(), errors=('raw_ledger:missing',),
            _acquisition=gate._acquisition)
        try:
            with pytest.raises(AdmissionUnavailable, match='incomplete'):
                relay.start_for_production(cut=cut, gate=gate)
            assert relay.broker_pid is None
        finally:
            relay.close(timeout=2)


def test_production_start_refuses_missing_gate(tmp_path):
    import pytest
    from hermes_cli.serve_producer_admission import AdmissionUnavailable
    relay = ProjectionRelay({}, home=tmp_path, runtime=tmp_path / 'runtime', generation='g')
    with pytest.raises(AdmissionUnavailable, match='gate-held'):
        relay.start_for_production(cut=None, gate=None)


def test_production_start_boots_after_complete_cut(tmp_path):
    import hashlib
    import pytest
    pytest.importorskip('select')
    path = tmp_path / 'ok.py'
    path.write_text('def produce(payload):\n    return payload\n', encoding='utf-8')
    relay = ProjectionRelay({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                             'manifest': {'version': 1, 'callbacks': {'done': [], 'waiting': []},
                                          'providers': {'ok': {'path': 'ok.py', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}},
                                          'modes': {}, 'facts': {}}},
                            home=tmp_path, runtime=tmp_path / 'runtime', generation='g1',
                            initial={'ok': True})
    gate = LaunchGate.acquire(tmp_path / 'runtime')
    cut = _complete_cut(gate, tmp_path)
    assert cut.complete
    try:
        relay.start_for_production(cut=cut, gate=gate)
        assert relay.ping()['verb'] == 'PONG'
    finally:
        assert relay.retire(timeout=10)  # the shared owner is detached from the starting serve
