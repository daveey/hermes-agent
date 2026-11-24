"""Holder-only waiting reuse; declaration keys never suppress strict request reads."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_annotation_parts as parts
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_annotation_parts as helpers

world = helpers.world


def refresh():
    host._event_refresh(SimpleNamespace(replace=lambda *a, **kw: None, control_only=False))


@pytest.fixture
def waiting(world, monkeypatch):
    state = SimpleNamespace(reads=0, key=0, ids={'t1'}, fail=False)

    def source():
        state.reads += 1
        if state.fail:
            raise RuntimeError('source failed')
        return {'s1': set(state.ids)}

    monkeypatch.setattr(ksa, '_waiting_sources', {'fixture': source})
    monkeypatch.setattr(host, '_declared', {'fixture': (source, lambda: state.key)})
    monkeypatch.setattr(host, '_event_sources', {'fixture': (source, list, lambda: None)})
    return state


def test_declared_waiting_skips_unchanged_wakes_but_not_requests(waiting):
    refresh()
    assert waiting.reads == 1
    refresh()
    refresh()
    assert waiting.reads == 1
    assert ksa._extra_waiting() == {'s1': {'t1'}}
    assert waiting.reads == 2, 'ordinary callers must retain their literal read'
    waiting.key += 1
    waiting.ids = {'t2'}
    refresh()
    assert waiting.reads == 3
    assert ksa._last_annotation[helpers.SCOPE][9]['s1'] == frozenset({'t2'})


def test_clock_and_failed_scope_invalidate_waiting(waiting):
    refresh()
    host._clock_deadlines[helpers.SCOPE] = [0.0]
    refresh()
    assert waiting.reads == 2
    parts.failed(helpers.SCOPE)
    refresh()
    assert waiting.reads == 3


def test_undeclared_and_unreadable_keys_always_read(waiting, monkeypatch):
    refresh()
    monkeypatch.setattr(host, '_declared', {})
    refresh()
    refresh()
    assert waiting.reads == 3

    def broken():
        raise RuntimeError('key unavailable')

    fn = ksa._waiting_sources['fixture']
    monkeypatch.setattr(host, '_declared', {'fixture': (fn, broken)})
    refresh()
    refresh()
    assert waiting.reads == 5


def test_source_replacement_with_same_code_and_key_reads(waiting, monkeypatch):
    refresh()
    old = ksa._waiting_sources['fixture']

    def replacement():
        waiting.reads += 1
        return {'s2': {'other'}}

    monkeypatch.setattr(host, '_code_id', lambda fn: 'same-code')
    refresh()
    before = waiting.reads
    ksa._waiting_sources['fixture'] = replacement
    host._declared['fixture'] = (replacement, lambda: waiting.key)
    host._event_sources['fixture'] = (replacement, list, lambda: None)
    refresh()
    assert waiting.reads == before + 1
    assert old is not replacement


def test_waiting_failure_cannot_certify_or_reuse_previous_output(waiting):
    refresh()
    waiting.key += 1
    waiting.fail = True
    refresh()
    assert waiting.reads == 2
    waiting.fail = False
    waiting.ids = {'recovered'}
    refresh()
    assert waiting.reads == 3
    refresh()
    assert waiting.reads == 3


def test_holder_waiting_output_is_detached_scoped_and_bounded(world, monkeypatch):
    monkeypatch.setattr(parts, '_MAX_SCOPES', 2)
    context = ksa._revalidate.force_read
    monkeypatch.setattr(context, 'parts', frozenset(), raising=False)
    value = {'s': {'original'}}
    reads = []

    def read():
        reads.append(True)
        return value

    def get(scope):
        monkeypatch.setattr(ksa, '_origin_scope', lambda: scope)
        monkeypatch.setattr(context, 'scope', scope, raising=False)
        return parts.waiting(read)

    a, b, c = ('a',), ('b',), ('c',)
    answer = get(a)
    answer['s'].add('caller')
    value['s'].add('provider')
    assert get(a) == {'s': {'original'}}
    assert get(b) == value
    assert get(a) == {'s': {'original'}}
    assert len(reads) == 2
    get(c)
    assert len(parts._waiting) == 2
    get(a)  # A was evicted; absence cannot certify an empty answer.
    assert len(reads) == 4
    parts.reset()
    assert not parts._waiting


def test_key_captured_before_read_does_not_absorb_a_racing_change(waiting, monkeypatch):
    original = ksa._waiting_sources['fixture']

    def racing():
        result = original()
        waiting.key += 1
        waiting.ids = {'next'}
        return result

    monkeypatch.setattr(ksa, '_waiting_sources', {'fixture': racing})
    monkeypatch.setattr(host, '_declared', {'fixture': (racing, lambda: waiting.key)})
    monkeypatch.setattr(host, '_event_sources', {'fixture': (racing, list, lambda: None)})
    refresh()
    refresh()
    assert waiting.reads == 2
    assert ksa._last_annotation[helpers.SCOPE][9] == {'s1': frozenset({'next'})}
