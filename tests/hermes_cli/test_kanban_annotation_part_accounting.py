"""Part costs distinguish a forced read from an ordinary cache-eligible call."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_annotation_parts as parts
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import loop_accounting as la


@pytest.mark.parametrize('forced', [True, False])
def test_part_read_counts_and_preserves_result(monkeypatch, forced):
    scope = ('part-accounting',)
    monkeypatch.setattr(ksa, '_origin_scope', lambda: scope)
    monkeypatch.setattr(ksa._revalidate.force_read, 'scope', scope, raising=False)
    monkeypatch.setattr(ksa._revalidate.force_read, 'parts', {parts.COUNTS} if forced else set(), raising=False)
    key = 'annotation.part.counts.' + ('forced' if forced else 'served')
    before = la.counters().get(key, {}).get('runs', 0)
    value = object()
    assert parts.measured(parts.COUNTS, lambda: value) is value
    assert la.counters()[key]['runs'] == before + 1


def test_host_measures_each_part(monkeypatch):
    from hermes_cli import kanban_annotation_host as host

    monkeypatch.setattr(ksa, '_all_counts', lambda **kw: ({},) * 9)
    monkeypatch.setattr(ksa, '_extra_waiting', dict)
    monkeypatch.setattr(ksa, 'attention_task_counts', dict)
    monkeypatch.setattr(ksa, '_origin_scope', lambda: ('host-accounting',))
    calls = []

    def measured(part, read):
        calls.append(part)
        return read()

    monkeypatch.setattr(parts, 'measured', measured)
    monkeypatch.setattr(host, 'frozen_parts', {})
    assert len(host.read_annotation_parts()) == 12
    assert calls == [parts.COUNTS, 'waiting', parts.ATTENTION]


def test_part_read_accounts_failure_without_swallowing(monkeypatch):
    monkeypatch.setattr(ksa, '_origin_scope', lambda: ('accounting-failure',))
    key = 'annotation.part.attention.served'
    before = la.counters().get(key, {}).get('errors', 0)

    def failed():
        raise ValueError('part unavailable')

    with pytest.raises(ValueError, match='part unavailable'):
        parts.measured(parts.ATTENTION, failed)
    assert la.counters()[key]['errors'] == before + 1
