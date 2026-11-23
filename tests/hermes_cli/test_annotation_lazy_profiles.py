"""Lazy per-profile annotation capture: unseeded profiles register nothing until demanded."""
import threading

import pytest

from hermes_cli import fact_annotation_runtime as runtime_module
from hermes_cli import kanban_annotation_facts as annotations
from hermes_cli.fact_annotation_runtime import AnnotationRuntime
from hermes_cli.fact_annotation_startup import binding_for
from hermes_cli.fact_store import FactStore
from tests.hermes_cli import test_kanban_session_activity as helpers

kanban_root = helpers.kanban_root


class FakeProcess:
    """Records coalesced submissions; the test runs them like the control worker would."""
    def __init__(self):
        self._store, self.queue = FactStore(), {}
        self.submits = 0

    def store(self):
        return self._store

    def submit(self, key, callback):
        self.submits += 1
        self.queue.setdefault(key, callback)

    def drain(self):
        queue, self.queue = self.queue, {}
        for callback in queue.values():
            callback()


@pytest.fixture
def harness(kanban_root, tmp_path, monkeypatch):
    process = FakeProcess()
    monkeypatch.setattr(runtime_module, '_process', lambda: process)
    built, legacy = [], []
    real = AnnotationRuntime._sources

    def counting(engine, scope, home, gateway):
        built.append(scope.profile_id)
        return real(engine, scope, home, gateway)
    monkeypatch.setattr(AnnotationRuntime, '_sources', staticmethod(counting))
    homes = {}
    for name in ('default', 'named'):
        homes[name] = tmp_path / name
        homes[name].mkdir()
    runtime = AnnotationRuntime(homes, fallback_profile='default', gateway='fixture',
        stop_legacy=lambda: legacy.append('stop'), start_legacy=lambda: legacy.append('start'))
    try:
        yield runtime, process, built, legacy
    finally:
        runtime.close()


def _rows(profile):
    return [{'id': 'row', 'profile': profile}]


def _legacy(rows, **kwargs):
    raise AssertionError('legacy I/O must never run')


def test_construction_is_inert(harness):
    runtime, process, built, legacy = harness
    assert built == [] and runtime.handles == {} and runtime.owners == {}
    assert process.submits == 0 and legacy == []
    assert all(binding._mode == 'legacy' for binding in runtime.bindings.values())


def test_no_seeds_no_capture_and_lookup_fails_closed_then_activates(harness):
    runtime, process, built, legacy = harness
    runtime.start({'default': [], 'named': []}, timeout=1)
    try:
        assert built == [] and runtime.handles == {} and legacy == []
        assert binding_for('named').scope.profile_id == 'named'
        assert binding_for('absent') is None
        for _ in range(3):
            with pytest.raises(annotations.AnnotationUnavailable):
                annotations.annotate('goal', _rows('named'), _legacy)
        # Request path: memory only, coalesced to one activation, no source I/O.
        assert list(process.queue) == [('fact_annotation_runtime.activate', id(runtime), 'named')]
        assert built == [] and runtime.handles == {}
        process.drain()
        assert built == ['named'] and list(runtime.handles) == ['named']
        assert legacy == ['stop']
        assert runtime.bindings['default'].real is None
        # Active but no projection yet: still unavailable, now via the real binding + demand.
        with pytest.raises(annotations.AnnotationUnavailable):
            annotations.annotate('goal', _rows('named'), _legacy)
        assert process.queue == {}
        with pytest.raises(annotations.AnnotationUnavailable):
            annotations.annotate('goal', _rows('default'), _legacy)
        assert list(process.queue) == [('fact_annotation_runtime.activate', id(runtime), 'default')]
    finally:
        runtime.close()
    assert binding_for() is None
    assert legacy == ['stop', 'start']  # resumed once, after the last native profile closed
    runtime.close()
    assert legacy == ['stop', 'start']


def test_only_seeded_profile_registers_sources(harness):
    runtime, process, built, legacy = harness
    with pytest.raises(TimeoutError):
        runtime.start({'default': [], 'named': _rows('named')}, timeout=.05)
    assert built == ['named'] and list(runtime.handles) == ['named']
    assert legacy == [] and binding_for() is None
    assert process.submits == 0


def test_activation_failure_stays_unavailable_and_backs_off(harness, monkeypatch):
    runtime, process, _built, _legacy_calls = harness
    runtime.start({'default': [], 'named': []}, timeout=1)
    calls = []

    def boom(*args):
        calls.append(args)
        raise RuntimeError('boom')
    monkeypatch.setattr(runtime.local, 'acquire', lambda *a, **k: boom())
    with pytest.raises(annotations.AnnotationUnavailable):
        annotations.annotate('goal', _rows('named'), _legacy)
    with pytest.raises(RuntimeError):
        process.drain()
    assert runtime.handles == {} and runtime.owners == {} and runtime.bindings['named'].real is None
    # Backoff: an immediate re-demand does not enqueue another attempt.
    with pytest.raises(annotations.AnnotationUnavailable):
        annotations.annotate('goal', _rows('named'), _legacy)
    assert process.queue == {}
    runtime._retry_at.clear()
    monkeypatch.undo()
    with pytest.raises(annotations.AnnotationUnavailable):
        annotations.annotate('goal', _rows('named'), _legacy)
    assert len(process.queue) == 1 and len(calls) == 1


def test_failed_owner_close_retains_custody_and_blocks_replacement(harness, monkeypatch):
    runtime, _process, built, _legacy_calls = harness
    runtime.start({'default': [], 'named': []}, timeout=1)
    runtime._activate('named')
    owner = runtime.owners['named']
    handle = runtime.handles['named']
    close = owner.close
    attempts = []

    def fail_close():
        attempts.append(owner)
        raise RuntimeError('owner still running')

    monkeypatch.setattr(owner, 'close', fail_close)
    try:
        with pytest.raises(RuntimeError, match='owner still running'):
            runtime._teardown('named')
        assert runtime.owners.get('named') is owner
        assert runtime.handles.get('named') is handle
        with pytest.raises(annotations.AnnotationUnavailable):
            annotations.annotate('goal', _rows('named'), _legacy)
        runtime._retry_at.clear()
        with pytest.raises(RuntimeError, match='owner still running'):
            runtime._activate('named')
        assert built == ['named']
        assert len(attempts) == 2
        assert runtime.owners.get('named') is owner
        assert runtime.handles.get('named') is handle
        with pytest.raises(RuntimeError, match='owner still running'):
            runtime.close()
        assert not runtime._closed
        assert runtime.owners.get('named') is owner
    finally:
        monkeypatch.setattr(owner, 'close', close)
        runtime.close()
    assert runtime._closed and runtime.owners == {} and runtime.handles == {}


def test_activation_binding_never_delegates_to_legacy_before_certification(harness, monkeypatch):
    runtime, _process, _built, _legacy_calls = harness
    runtime.start({'default': [], 'named': []}, timeout=1)
    certify = runtime._certify
    observed = []

    def during_certification(profile):
        # Deterministically exercise the interval after consumer registration but
        # before the lifecycle callback has switched it from its default mode.
        with pytest.raises(annotations.AnnotationUnavailable):
            annotations.annotate('goal', _rows(profile), _legacy)
        observed.append(profile)
        certify(profile)

    monkeypatch.setattr(runtime, '_certify', during_certification)
    runtime._activate('named')
    assert observed == ['named']
    assert runtime.bindings['named']._mode == 'facts'
    with pytest.raises(annotations.AnnotationUnavailable):
        annotations.annotate('goal', _rows('named'), _legacy)


@pytest.mark.parametrize('failure_stage', ['acquire', 'certify'])
def test_failed_activation_cleanup_must_finish_before_retry(harness, monkeypatch, failure_stage):
    runtime, process, built, _legacy_calls = harness
    runtime.start({'default': [], 'named': []}, timeout=1)
    acquire, certify = runtime.local.acquire, runtime._certify
    owner_close = runtime_module.NativeAnnotationOwner.close
    retained = []

    def fail_activation(*args, **kwargs):
        raise RuntimeError('activation failed')

    def fail_close(owner):
        retained.append(owner)
        raise RuntimeError('cleanup failed')

    monkeypatch.setattr(runtime_module.NativeAnnotationOwner, 'close', fail_close)
    if failure_stage == 'acquire':
        monkeypatch.setattr(runtime.local, 'acquire', fail_activation)
    else:
        monkeypatch.setattr(runtime, '_certify', fail_activation)
    try:
        with pytest.raises(RuntimeError, match='cleanup failed'):
            runtime._activate('named')
        owner = retained[0]
        assert runtime.owners.get('named') is owner
        assert built == ['named']
        runtime._retry_at.clear()
        with pytest.raises(annotations.AnnotationUnavailable):
            annotations.annotate('goal', _rows('named'), _legacy)
        with pytest.raises(RuntimeError, match='cleanup failed'):
            process.drain()
        assert built == ['named']
        assert retained == [owner, owner]
        assert runtime.owners.get('named') is owner
    finally:
        monkeypatch.setattr(runtime_module.NativeAnnotationOwner, 'close', owner_close)
        monkeypatch.setattr(runtime.local, 'acquire', acquire)
        monkeypatch.setattr(runtime, '_certify', certify)
    # Cleanup succeeds before a replacement is admitted. Reuse the same reducer
    # engine and lifecycle; duplicate registration or stale generations fail here.
    runtime._activate('named')
    assert built == ['named', 'named']
    assert runtime.owners['named'] is not owner
    assert runtime.bindings['named']._mode == 'facts'
    assert runtime.bindings['named'].real is not None


def test_activation_after_close_is_a_noop(harness):
    runtime, process, built, _legacy_calls = harness
    runtime.start({'default': [], 'named': []}, timeout=1)
    with pytest.raises(annotations.AnnotationUnavailable):
        annotations.annotate('goal', _rows('named'), _legacy)
    runtime.close()
    process.drain()
    assert built == [] and runtime.handles == {}


def test_submit_failure_never_falls_back_to_legacy(harness, monkeypatch):
    runtime, process, built, legacy = harness
    runtime.start({'default': [], 'named': []}, timeout=1)

    def full(key, callback):
        raise RuntimeError('control queue full')
    monkeypatch.setattr(process, 'submit', full)
    with pytest.raises(annotations.AnnotationUnavailable):
        annotations.annotate('goal', _rows('named'), _legacy)
    assert built == [] and legacy == []


def test_concurrent_demand_builds_once(harness):
    runtime, process, built, _legacy_calls = harness
    runtime.start({'default': [], 'named': []}, timeout=1)
    barrier = threading.Barrier(4)

    def demand():
        barrier.wait()
        try:
            annotations.annotate('goal', _rows('named'), _legacy)
        except annotations.AnnotationUnavailable:
            pass
    threads = [threading.Thread(target=demand) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(process.queue) == 1
    process.drain()
    process.drain()
    assert built == ['named']
