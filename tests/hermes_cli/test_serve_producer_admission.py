"""Admission cuts are usable only under the same held launch-gate acquisition."""
from dataclasses import replace

import pytest

from hermes_cli import serve_producer_admission as admission
from hermes_cli.serve_producer_census import Incarnation, ProcessCensus, ProcessObservation
from hermes_cli.serve_producer_custody import CustodyConflict


def _processes():
    identity = Incarnation('boot', 123, 42)
    process = ProcessObservation(identity, '/python', '/core', ('python',), ())
    return ProcessCensus('boot', 10, 11, (process,), (), ())


def _collectors():
    return {name: lambda: admission.CollectorEvidence(('positive-receipt',), ())
            for name in admission.REQUIRED_COLLECTORS}


def _classified(record):
    return admission.Classification(record.key, 'participant', 'verified-launch-receipt')


def _cut(gate, **kwargs):
    return gate.inspect(boot_id='boot', generation='generation', installation='/installation',
                        census=kwargs.get('census', _processes),
                        collectors=kwargs.get('collectors', _collectors()),
                        classify=kwargs.get('classify', _classified),
                        legacy_candidates=kwargs.get('legacy_candidates', ()),
                        exited=kwargs.get('exited', lambda identity: False))


@pytest.mark.platforms('posix')
def test_positive_cut_works_only_while_exact_gate_is_held(tmp_path):
    gate = admission.LaunchGate.acquire(tmp_path / 'runtime')
    cut = _cut(gate)
    gate.require(cut, boot_id='boot', generation='generation', installation='/installation')
    with pytest.raises(CustodyConflict):
        admission.LaunchGate.acquire(tmp_path / 'runtime')
    gate.close()
    with pytest.raises(admission.AdmissionUnavailable, match='held'):
        gate.require(cut, boot_id='boot', generation='generation', installation='/installation')
    with admission.LaunchGate.acquire(tmp_path / 'runtime') as successor:
        with pytest.raises(admission.AdmissionUnavailable, match='acquisition'):
            successor.require(cut, boot_id='boot', generation='generation', installation='/installation')


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('key,value', [('boot_id', 'new-boot'), ('generation', 'new-generation'),
                                       ('installation', '/different')])
def test_boot_installation_or_launcher_generation_change_invalidates_cut(tmp_path, key, value):
    with admission.LaunchGate.acquire(tmp_path / 'runtime') as gate:
        cut = _cut(gate)
        expected = dict(boot_id='boot', generation='generation', installation='/installation')
        expected[key] = value
        with pytest.raises(admission.AdmissionUnavailable, match='identity'):
            gate.require(cut, **expected)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('failure', ['missing', 'exception', 'no_proof', 'conflict'])
def test_every_required_collector_must_prove_success(tmp_path, failure):
    collectors = _collectors()
    key = next(iter(collectors))
    if failure == 'missing':
        del collectors[key]
    elif failure == 'exception':
        def denied():
            raise PermissionError('not absence')
        collectors[key] = denied
    elif failure == 'no_proof':
        collectors[key] = lambda: admission.CollectorEvidence((), ())
    else:
        collectors[key] = lambda: admission.CollectorEvidence(('receipt',), ('unknown socket owner',))
    with admission.LaunchGate.acquire(tmp_path / 'runtime') as gate:
        cut = _cut(gate, collectors=collectors)
        assert not cut.complete
        assert any(key in error for error in cut.errors)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('kind', ['legacy', 'unknown'])
def test_unknown_python_and_legacy_snapshots_never_admit(tmp_path, kind):
    with admission.LaunchGate.acquire(tmp_path / 'runtime') as gate:
        cut = _cut(gate, classify=lambda record: admission.Classification(record.key, kind, '/snapshot'))
        assert not cut.complete
        with pytest.raises(admission.AdmissionUnavailable, match='incomplete'):
            gate.require(cut, boot_id='boot', generation='generation', installation='/installation')


@pytest.mark.platforms('posix')
def test_legacy_spawner_must_be_affirmatively_retired_not_missing_from_census(tmp_path):
    legacy = ProcessObservation(Incarnation('boot', 456, 7), '/python', '/old', ('python',), ()).key
    with admission.LaunchGate.acquire(tmp_path / 'runtime') as gate:
        assert not _cut(gate, legacy_candidates=(legacy,)).complete
        assert _cut(gate, legacy_candidates=(legacy,), exited=lambda value: value == legacy).complete
        def denied(identity):
            raise PermissionError('not exit')
        assert not _cut(gate, legacy_candidates=(legacy,), exited=denied).complete


@pytest.mark.platforms('posix')
def test_empty_best_effort_and_stale_census_are_not_positive_proof(tmp_path):
    with admission.LaunchGate.acquire(tmp_path / 'runtime') as gate:
        cut = _cut(gate, census=lambda: replace(_processes(), errors=('ledger:missing',)))
        assert not cut.complete
        assert not _cut(gate, census=lambda: replace(_processes(), boot_id='old-boot')).complete
        assert not _cut(gate, classify=lambda record: admission.Classification(
            replace(record.key, start_ticks=41), 'participant', 'wrong-incarnation')).complete
