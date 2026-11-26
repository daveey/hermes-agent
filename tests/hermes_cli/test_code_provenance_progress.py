"""Finite private scalar recorder and legacy snapshot-call compatibility."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from hermes_cli import code_attestation as ca
from hermes_cli import code_attestation_snapshot as projection
from hermes_cli import code_provenance_progress as pp
from tests.hermes_cli import test_code_attestation_snapshot as helpers

ledger = helpers.ledger


def test_fixed_scalar_detached_samples_and_finite_transition_limit(monkeypatch):
    clocks = SimpleNamespace(mono=0, cpu=100)
    monkeypatch.setattr(pp, 'time', SimpleNamespace(monotonic_ns=lambda: clocks.mono,
                                                 thread_time_ns=lambda: clocks.cpu,
                                                 process_time_ns=lambda: clocks.cpu * 10 + 9000000))
    progress = pp.CaptureProgress()
    progress.set_phase('worker_start')
    first = progress.snapshot()
    assert first['phase'] == 'worker_start'
    clocks.mono, clocks.cpu = 5000000, 7100100
    progress.set_phase('disk')
    sample = progress.snapshot()
    assert sample == {'schema': 1, 'phase': 'disk', 'sampled_elapsed_ms': 5,
                      'worker_cpu_ms': 7, 'cpu_is_lower_bound': True}
    sample['phase'] = 'unavailable'
    assert progress.snapshot()['phase'] == 'disk'
    assert first['phase'] == 'worker_start' and first['worker_cpu_ms'] == 0
    for _ in range(7):
        progress.set_phase('inventory')
    with pytest.raises(ValueError, match='capture_phase_limit'):
        progress.set_phase('complete')
    assert not hasattr(progress, '__dict__')
    assert len(json.dumps(progress.snapshot()).encode()) < 4096


@pytest.mark.parametrize('phase', ['SECRET_SENTINEL', ['disk'], 1, None])
def test_phase_validation(phase):
    with pytest.raises(ValueError, match='invalid_capture_phase'):
        pp.CaptureProgress().set_phase(phase)


@pytest.mark.parametrize('failure', [LookupError, ArithmeticError])
def test_recorder_startup_failure_settles_and_releases_flight(monkeypatch, failure):
    from gateway import code_provenance_control as control
    runtime = SimpleNamespace()
    capture = control.CodeProvenanceControl(runtime)
    monkeypatch.setattr(capture, 'identity', lambda: {'pid': 1})
    def fail():
        raise failure('SECRET_SENTINEL')
    monkeypatch.setattr(control, 'CaptureProgress', fail)
    try:
        result = capture.capture({'expected': {'pid': 1}}, 'fixture-peer')
        assert result == {**control._error('capture_failed'), 'diagnostics': pp.unavailable()}
        assert not control._FLIGHT.locked()
    finally:
        # A missing-containment mutant must not strand later fixture captures.
        if control._FLIGHT.locked():
            control._FLIGHT.release()


def test_phase_subclass_rejected_even_with_valid_enum_value():
    class Phase(str):
        pass
    with pytest.raises(ValueError, match='invalid_capture_phase'):
        pp.CaptureProgress().set_phase(Phase('disk'))


@pytest.mark.parametrize('negative', [False, True])
def test_counters_clamp_without_duration_dependent_encoding(monkeypatch, negative):
    clocks = SimpleNamespace(mono=0, cpu=0)
    monkeypatch.setattr(pp, 'time', SimpleNamespace(monotonic_ns=lambda: clocks.mono,
                                                 thread_time_ns=lambda: clocks.cpu,
                                                 process_time_ns=lambda: clocks.cpu * 10 + 9000000))
    progress = pp.CaptureProgress()
    progress.set_phase('worker_start')
    clocks.mono = clocks.cpu = -10**100 if negative else 10**100
    progress.set_phase('complete')
    expected = 0 if negative else pp.MAX_COUNTER_MS
    assert progress.snapshot()['sampled_elapsed_ms'] == expected
    assert progress.snapshot()['worker_cpu_ms'] == expected
    assert len(json.dumps(progress.snapshot()).encode()) < 4096


def test_snapshot_is_nonblocking_and_never_reads_clocks_under_lock(monkeypatch):
    progress = pp.CaptureProgress()
    def mono():
        assert not progress._lock.locked()
        return 1
    monkeypatch.setattr(pp, 'time', SimpleNamespace(monotonic_ns=mono, thread_time_ns=mono))
    progress.set_phase('worker_start')
    progress._lock.acquire()
    results = []
    reader = threading.Thread(target=lambda: results.append(progress.snapshot()))
    reader.start()
    reader.join(1)
    was_alive = reader.is_alive()
    progress._lock.release()
    reader.join(2)
    assert not was_alive, 'snapshot waited on recorder lock'
    assert results == [{**pp.unavailable(), 'inventory': {'schema': 1, 'stage': 'unavailable', 'completed': 0,
                       'sampled_elapsed_ms': 0, 'worker_cpu_ms': 0, 'cpu_is_lower_bound': True}}]
    results[0]['phase'] = 'disk'
    assert pp.unavailable()['phase'] == 'unavailable'


def test_facade_preserves_old_replacement_signature_and_forwards_optional(ledger, monkeypatch):
    sentinel = object()
    def old(ledger_arg, *, deadline):
        assert ledger_arg is ledger and deadline == 12
        return sentinel
    monkeypatch.setattr(projection, 'snapshot', old)
    assert ca.diagnostic_snapshot(deadline=12) is sentinel
    progress = pp.CaptureProgress()
    def new(ledger_arg, *, deadline, progress):
        assert ledger_arg is ledger and deadline == 12 and progress is recorder
        return sentinel
    recorder = progress
    monkeypatch.setattr(projection, 'snapshot', new)
    assert ca.diagnostic_snapshot(deadline=12, progress=progress) is sentinel


def test_all_real_boundaries_and_none_preserve_canonical_snapshot(ledger, monkeypatch):
    fixed_time = time.monotonic()
    monkeypatch.setattr(projection, 'time', SimpleNamespace(monotonic=lambda: fixed_time))
    ledger._reviewed_plugins = {'fixture': True}
    from hermes_cli import plugin_review_records
    monkeypatch.setattr(plugin_review_records, 'verify_registered', lambda ledger: None)
    class Observer:
        def __init__(self):
            self.phases = []
        def set_phase(self, phase):
            self.phases.append(phase)
    observer = Observer()
    plain = projection.snapshot(ledger, deadline=fixed_time + 2)
    instrumented = projection.snapshot(ledger, deadline=fixed_time + 2, progress=observer)
    assert plain == instrumented
    assert observer.phases == ['disk', 'ledger_lock', 'inventory', 'registry', 'projection', 'finalize', 'complete']
    ledger._reviewed_plugins.clear()
    observer.phases.clear()
    projection.snapshot(ledger, deadline=fixed_time + 2, progress=observer)
    assert observer.phases == ['disk', 'ledger_lock', 'inventory', 'projection', 'finalize', 'complete']


def test_complete_is_not_sampled_if_final_deadline_fails(ledger, monkeypatch):
    progress = pp.CaptureProgress()
    original = projection._Builder.finish
    def expired(builder, *args):
        builder.deadline = 0
        return original(builder, *args)
    monkeypatch.setattr(projection._Builder, 'finish', expired)
    with pytest.raises(TimeoutError):
        ca.diagnostic_snapshot(deadline=time.monotonic() + 2, progress=progress)
    assert progress.snapshot()['phase'] == 'finalize'
