"""Own-source holder receipts: request generations cannot renew retained evidence."""
from __future__ import annotations

import copy
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_annotation_counts as counts
from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_annotation_parts as parts
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_annotation_parts as holders

world = holders.world


@pytest.fixture
def clock(world, monkeypatch):
    wall, mono = time.time(), time.monotonic()
    state = {'offset': 0.0}
    fake = SimpleNamespace(time=lambda: wall + state['offset'], monotonic=lambda: mono + state['offset'])
    monkeypatch.setattr(ksa, 'time', fake)
    monkeypatch.setattr(host, 'time', fake)
    monkeypatch.setattr(parts, 'time', fake)
    original = host._inputs_now
    def inputs(scope):
        local, full, _wall, fp = original(scope)
        return local, full, fake.time(), fp
    monkeypatch.setattr(host, '_inputs_now', inputs)
    return state, fake


def test_requests_cannot_extend_own_source_renewal(world, clock, monkeypatch):
    state, fake = clock
    saved = []
    monkeypatch.setattr(host._disk, 'save_host', lambda scope, digest, at, value, **kw: saved.append((at, value)) or True)
    holders._refresh()
    first = counts._entries[holders.SCOPE]
    assert first.snapshot.execution and saved
    for delta in (31.0, 62.0, 93.0):
        state['offset'] = delta
        with ksa._annotate_refresh_lock:
            ksa._refresh_annotation_locked(holders.SCOPE)
        shared_generation = ksa._cache_generation
        old_reads = world.counts
        world.declared.write_text(str(delta))
        holders._refresh()
        source = counts._entries[holders.SCOPE]
        assert source.mono == fake.monotonic()
        assert source.raw[4] > shared_generation
        assert world.counts == old_reads + 1
        assert saved[-1][0] == pytest.approx(fake.time())
        assert ksa._bounded.read_at(holders._published()) == pytest.approx(source.wall)
    assert counts._entries[holders.SCOPE].mono - first.mono == 93.0


def test_reuse_never_restamps_exact_restart_or_local_source(world, clock, monkeypatch):
    state, _ = clock
    saved, restarted = [], []
    monkeypatch.setattr(host._disk, 'save_host', lambda *a, **kw: saved.append((a, kw)) or True)
    monkeypatch.setattr(ksa._annotation_copy, 'published', lambda *a: restarted.append(a))
    holders._refresh()
    source = counts._entries[holders.SCOPE]
    original_execution = copy.deepcopy(source.snapshot.execution)
    assert original_execution
    # An ordinary request changes neither holder custody nor its source date.
    state['offset'] = 5.0
    with ksa._annotate_refresh_lock:
        ksa._refresh_annotation_locked(holders.SCOPE)
    assert counts._entries[holders.SCOPE] is source
    saved.clear()
    restarted.clear()
    world.flagged = False
    world.declared.write_text('attention-only')
    before = world.counts
    holders._refresh()
    result = holders._published()
    assert world.counts == before and result[10] == {}
    assert result[11] == original_execution
    assert counts._entries[holders.SCOPE] is source
    assert ksa._bounded.read_at(result) == source.wall
    assert saved == [] and restarted == []
    assert not counts.reused(), 'per-read custody leaked beyond refresh finally'


def test_detached_full_snapshot_and_execution_do_not_alias_requests(world):
    holders._refresh()
    source = counts._entries[holders.SCOPE]
    assert source.snapshot.execution
    original = copy.deepcopy(source.snapshot.execution)
    ksa._execution_cache[0].clear()
    assert source.snapshot.execution == original
    assert source.snapshot.execution is not ksa._execution_cache[0]
    # Mutating a returned detached object cannot change the next use.
    forced, _keys = parts.plan(holders.SCOPE, clock=False)
    ksa._revalidate.force_read.scope = holders.SCOPE
    ksa._revalidate.force_read.parts = forced
    try:
        value = counts.read(lambda: pytest.fail('unexpected actual read'))
        assert tuple(value) == tuple(source.snapshot) and value.execution == original
        value.execution.clear()
        again = counts.read(lambda: pytest.fail('unexpected actual read'))
        assert again.execution == original
    finally:
        ksa._revalidate.force_read.scope = None
        ksa._revalidate.force_read.parts = None
        counts.clear_use()


@pytest.mark.parametrize('operation', ['failed', 'reset', 'evicted', 'incomplete'])
def test_missing_or_failed_receipt_forces_even_without_other_changes(world, operation):
    holders._refresh()
    before = world.counts
    if operation == 'failed':
        parts.failed(holders.SCOPE)
    elif operation == 'reset':
        counts.reset()
    elif operation == 'evicted':
        counts._entries.pop(holders.SCOPE)
    else:
        counts._entries.pop(holders.SCOPE)
        ksa._cache_revalidatable = False
    holders._refresh()
    assert world.counts == before + 1
    assert holders.SCOPE in counts._entries


@pytest.mark.parametrize('kind', ['mono-backward', 'wall-backward', 'mutation', 'deadline', 'lost'])
def test_invalid_clock_mutation_or_coverage_never_reuses(world, clock, kind):
    _state, fake = clock
    holders._refresh()
    source = counts._entries[holders.SCOPE]
    actions = {
        'mono-backward': lambda: counts._entries.update({holders.SCOPE: replace(source, mono=fake.monotonic() + 1)}),
        'wall-backward': lambda: counts._entries.update({holders.SCOPE: replace(source, wall=fake.time() + 1)}),
        'mutation': lambda: host._mutation.record(holders.SCOPE),
        'deadline': lambda: host._clock_deadlines.update({holders.SCOPE: [fake.time() - 1]}),
        'lost': lambda: None,
    }
    actions[kind]()
    before = world.counts
    if kind == 'lost':
        host._event_refresh(SimpleNamespace(replace=lambda *a, **kw: None, coverage_lost=True, control_only=False))
    else:
        holders._refresh()
    assert world.counts == before + 1


def test_read_failure_clears_receipt_context_and_later_forces(world, monkeypatch):
    holders._refresh()
    before = world.counts
    world.conn.execute("UPDATE tasks SET status='done' WHERE id='t1'")
    original = ksa._all_counts
    def fail(**kwargs):
        raise RuntimeError('fixture read failure')
    monkeypatch.setattr(ksa, '_all_counts', fail)
    holders._refresh()
    assert holders.SCOPE not in counts._entries
    assert not counts.reused() and getattr(counts._use, 'source', None) is None
    monkeypatch.setattr(ksa, '_all_counts', original)
    holders._refresh()
    assert world.counts == before + 1 and holders._published()[0] == {}


def test_late_older_request_flight_cannot_certify_new_plan(world, clock, monkeypatch):
    state, _fake = clock
    holders._refresh()
    fields = {name: getattr(ksa, name) for name in ksa._count_scopes._FIELDS}
    boxes = {name: getattr(ksa, name)[0] for name in ksa._count_scopes._BOXES}
    state['offset'] = 1.0
    world.conn.execute("UPDATE tasks SET status='done' WHERE id='t1'")
    class LateFlight:
        def __enter__(self):
            # A request read A before this plan B but completes publication now.
            for name, value in fields.items():
                setattr(ksa, name, value)
            for name, value in boxes.items():
                getattr(ksa, name)[0] = value
            ksa._counts_generation += 1
            ksa._cache_generation = ksa._counts_generation

        def __exit__(self, *args):
            return False
    original = ksa._counts_flight
    monkeypatch.setattr(ksa, '_counts_flight', LateFlight())
    holders._refresh()  # actual _all_counts generation-advanced shortcut returns A
    assert holders._published()[0] == {'s1': 1}, 'fixture did not hit the late-flight shortcut'
    assert holders.SCOPE not in counts._entries, 'pre-plan A was certified under plan B'
    monkeypatch.setattr(ksa, '_counts_flight', original)
    holders._refresh()
    assert holders._published()[0] == {}
    assert holders.SCOPE in counts._entries


@pytest.mark.parametrize('cause', ['missing', 'mutation', 'future'])
def test_receipt_driven_read_disables_content_only_board_shortcut(world, clock, monkeypatch, cause):
    _state, fake = clock
    holders._refresh()
    source = counts._entries[holders.SCOPE]
    actions = {
        'missing': lambda: counts._entries.pop(holders.SCOPE),
        'mutation': lambda: host._mutation.record(holders.SCOPE),
        'future': lambda: counts._entries.update({holders.SCOPE: replace(source, wall=fake.time() + 1)}),
    }
    actions[cause]()
    seen = []
    original = ksa._recompute_all_counts
    def read(*args, **kwargs):
        seen.append(ksa._revalidate.force_read.content_only_boards)
        return original(*args, **kwargs)
    monkeypatch.setattr(ksa, '_recompute_all_counts', read)
    holders._refresh()
    assert seen == [False]
