"""Attention cache clock transitions under native annotation refresh."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_session_activity as ksa
from hermes_cli.kanban_session_activity_attention import AttentionCache


@pytest.fixture
def attention(monkeypatch):
    clock = SimpleNamespace(wall=1000.0, mono=100.0)
    monkeypatch.setattr(ksa, 'time', SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.mono))
    monkeypatch.setattr(ksa, '_attention', AttentionCache(30, max_age_s=300))
    monkeypatch.setattr(ksa, '_origin_scope', lambda: ('attention-events',))
    monkeypatch.setattr(ksa, '_board_db_paths', lambda: [Path('fixture.db')])
    monkeypatch.setattr(ksa._revalidate, 'attention_key', lambda: 'unchanged')
    monkeypatch.setattr(ksa._revalidate.force_read, 'scope', None, raising=False)
    rows = [('chat', {'id': 'task', 'completed_at': clock.wall - ksa.ATTENTION_WINDOW_S + 1,
                      'title': 'done', 'summary': 'check'})]
    monkeypatch.setattr(ksa._recent_done, 'memoized',
                        lambda path, since, *args: [(sid, card) for sid, card in rows
                                                   if card['completed_at'] >= since])
    flagged = [True]
    monkeypatch.setattr(ksa, '_done_checks', {'fixture': lambda card: flagged[0]})
    deadlines = []
    monkeypatch.setattr(host, 'note_deadline', deadlines.append)
    return clock, rows, flagged, deadlines


def test_attention_window_emits_deadline_even_on_cached_counts(attention):
    clock, _, _, deadlines = attention
    assert ksa.attention_task_counts() == {'chat': 1}
    assert deadlines and 1001.0 < deadlines[-1] < 1001.001
    first = deadlines[-1]
    deadlines.clear()
    clock.wall += .25
    clock.mono += .25
    assert ksa.attention_task_counts() == {'chat': 1}
    assert deadlines == [first], 'cache hit lost the window clock'


def test_attention_window_expires_inside_cache_ttl(attention):
    clock, _, _, _ = attention
    assert ksa.attention_task_counts() == {'chat': 1}
    clock.wall += 1.01
    clock.mono += 1.01
    assert ksa.attention_task_counts() == {}, 'TTL hid a completed card leaving the window'


def test_attention_window_is_inclusive_then_expires_at_emitted_deadline(attention):
    clock, _, _, deadlines = attention
    assert ksa.attention_task_counts() == {'chat': 1}
    due = deadlines[-1]
    clock.wall = 1001.0
    clock.mono = 101.0
    assert ksa.attention_task_counts() == {'chat': 1}
    clock.wall = due
    assert ksa.attention_task_counts() == {}, 'emitted wake still includes the expired row'


def test_attention_read_error_schedules_recovery_without_a_write(attention, monkeypatch):
    clock, rows, _, deadlines = attention
    rows[0][1]['completed_at'] = clock.wall
    original = ksa._recent_done.memoized

    def unavailable(*args):
        raise ksa.sqlite3.OperationalError('temporary source failure')

    monkeypatch.setattr(ksa._recent_done, 'memoized', unavailable)
    assert ksa.attention_task_counts() == {}
    retries = [due for due in deadlines if due is not None]
    assert retries and retries[-1] == clock.wall + host.HOLDER_INTERVAL_S
    clock.wall = retries[-1]
    clock.mono += host.HOLDER_INTERVAL_S
    monkeypatch.setattr(ksa._recent_done, 'memoized', original)
    monkeypatch.setattr(ksa._revalidate.force_read, 'scope', ('attention-events',))
    assert ksa.attention_task_counts() == {'chat': 1}


def test_window_state_is_bounded_replaced_and_cleared():
    cache = AttentionCache(30, max_scopes=1)
    cache.put(('a',), 0, {}, window=50)
    cache.put(('b',), 0, {}, window=60)
    assert cache.deadline(('a',)) is None
    cache.put(('b',), 1, {})
    assert cache.deadline(('b',)) is None
    cache.put(('b',), 2, {}, window=70)
    cache.clear()
    assert cache.deadline(('b',)) is None


def test_source_refresh_bypasses_counts_and_check_verdict(attention, monkeypatch):
    _, _, flagged, _ = attention
    assert ksa.attention_task_counts() == {'chat': 1}
    flagged[0] = False
    monkeypatch.setattr(ksa._revalidate.force_read, 'scope', ('attention-events',))
    assert ksa.attention_task_counts() == {}, 'source refresh reused the old done-check verdict'
