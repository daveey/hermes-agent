"""The kanban annotation capture is paced on the shared change feed. [daveey]

Hub 2026-11-24: the kanban family re-read every board (and published links) on each unrelated write to state.db,
the process registry, config or the kanban directory, on the shared ``fact-change-feed`` thread.
"""
import threading
from types import SimpleNamespace

import pytest

from hermes_cli.fact_annotation_sources import KANBAN_CAPTURE_INTERVAL, NativeAnnotationSource
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_feed import ChangeFeed
from hermes_cli.fact_process import _Adapter
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore
from tests.waiting import wait_until


def _source(tmp_path, family, calls):
    scope = Scope('install', 'profile')
    engine = ReducerEngine(FactStore(), scope)
    def read(budget, wall, ids):
        calls.append(family)
        return {'n': len(calls)}, wall + 30
    return NativeAnnotationSource(engine, SourceId(scope, 'annotations.' + family, 'local'),
                                  (tmp_path / 'state.db',), read)


@pytest.mark.parametrize('family,paced', [('kanban', True), ('background', False), ('turn', False),
                                          ('subagent', False), ('review', False)])
def test_only_the_expensive_kanban_family_is_paced(tmp_path, family, paced):
    source = _source(tmp_path, family, [])
    assert source.min_interval == (KANBAN_CAPTURE_INTERVAL if paced else 0)
    # The shared feed reads pacing through the owner's wrapper, which must forward it.
    assert _Adapter(source, SimpleNamespace(budget=None)).min_interval == source.min_interval


def test_a_burst_of_wakes_is_one_kanban_capture_not_one_per_hint(tmp_path):
    calls = []
    source = _source(tmp_path, 'kanban', calls)
    source.min_interval = 0.4  # same mechanism, test-sized interval
    feed = ChangeFeed(lambda *a: None, lambda *a: None)
    try:
        feed.register(source)
        assert wait_until(lambda: len(calls) == 1, timeout=5)  # registration's own pass
        for _ in range(200):
            source._wake()
        assert wait_until(lambda: len(calls) == 2, timeout=5)
        # health: allow HX013 -- the quiet interval after the trailing pass IS the behaviour under test
        threading.Event().wait(0.6)
        assert len(calls) == 2  # 200 hints cost one trailing pass
    finally:
        feed.close()
