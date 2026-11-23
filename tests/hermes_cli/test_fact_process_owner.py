"""Process-wide capture custody, context isolation and independent drains. [daveey]"""
from contextvars import ContextVar
from types import SimpleNamespace
import threading

from hermes_cli.fact_capture import CaptureBudget
from tests.waiting import wait_until


class Source:
    def __init__(self, seen, context):
        self.seen, self.context = seen, context
        self.closed = False

    def watch(self, wake):
        self.wake = wake
        return self

    def capture(self, cursor, budget):
        self.seen.append((self.context.get(), budget.max_seconds))
        return SimpleNamespace(after=1)

    def close(self):
        self.closed = True


def test_eleven_owners_share_one_feed_and_preserve_source_context():
    from hermes_cli.fact_process_owner import ProcessOwner
    context = ContextVar('fact-test-profile', default='foreign')
    owners, sources, committed = [], [], []
    try:
        for index in range(11):
            owner = ProcessOwner(lambda source, capture: committed.append(source), lambda *_: None,
                                 budget=CaptureBudget(max_seconds=index + 1))
            owners.append(owner)
            token = context.set(str(index))
            try:
                source = Source([], context)
                sources.append(source)
                owner.register(source)
            finally:
                context.reset(token)
        assert wait_until(lambda: len(committed) == 11)
        assert len({owner._thread for owner in owners}) == 1
        assert all(source.seen == [(str(index), index + 1)] for index, source in enumerate(sources))
        owners[0].close()
        assert sources[0].closed
        assert owners[1]._thread.is_alive()
        sources[1].wake()
        assert wait_until(lambda: len(sources[1].seen) == 2)
    finally:
        for owner in owners:
            owner.close()
    assert not owners[-1]._thread.is_alive()


def test_close_retains_capture_custody_until_actual_completion():
    from hermes_cli.fact_process_owner import ProcessOwner
    entered, release = threading.Event(), threading.Event()
    committed = []
    owner = ProcessOwner(lambda *args: committed.append(args), lambda *_: None)
    class Blocking(Source):
        def capture(self, cursor, budget):
            entered.set()
            assert release.wait(5)
            return SimpleNamespace(after=1)
    source = Blocking([], ContextVar('blocking-profile', default='test'))
    owner.register(source)
    try:
        assert entered.wait(5)
        import pytest
        with pytest.raises(TimeoutError):
            owner.close(timeout=.01)
        assert not source.closed
        assert owner._thread.is_alive()
    finally:
        release.set()
        owner.close()
    assert source.closed
    assert committed == []


def test_last_close_replaces_store_epoch_for_next_tenure():
    from hermes_cli.fact_process_owner import ProcessOwner, process_store
    first = process_store()
    owner = ProcessOwner(lambda *_: None, lambda *_: None)
    assert process_store() is first
    owner.close()
    second = process_store()
    assert second is not first
    assert second._epoch != first._epoch


def test_peer_close_does_not_wait_for_another_owners_capture():
    from hermes_cli.fact_process_owner import ProcessOwner
    entered, release = threading.Event(), threading.Event()
    first = ProcessOwner(lambda *_: None, lambda *_: None)
    peer = ProcessOwner(lambda *_: None, lambda *_: None)
    class Blocking(Source):
        def capture(self, cursor, budget):
            entered.set()
            assert release.wait(5)
            return SimpleNamespace(after=1)
    try:
        first.register(Blocking([], ContextVar('peer-blocking', default='test')))
        assert entered.wait(5)
        peer.close(timeout=.01)
        assert first._thread.is_alive()
    finally:
        release.set()
        first.close()
        peer.close()


def test_lazy_sources_watch_only_the_demanded_profile(tmp_path):
    from hermes_cli.fact_content_owner import ContentOwner
    from hermes_cli.fact_contract import Scope, SourceId, FactKey
    from hermes_cli.fact_store import FactStore
    from hermes_cli.fact_reducers import ReducerEngine
    from hermes_cli.fact_materializers import Projection, view_reducer
    scopes = [Scope(str(tmp_path), f'profile-{i}') for i in range(11)]
    engines = {scope: ReducerEngine(FactStore(), scope) for scope in scopes}
    watched = []
    class Operational(Source):
        def __init__(self, engine):
            self.engine = engine
            self.source = SourceId(engine.scope, 'test.input', 'native')
            self.key = FactKey(engine.scope, 'Input', '')
        def watch(self, wake):
            watched.append(self.source.scope)
            return super().watch(wake)
        def capture(self, cursor, budget):
            return SimpleNamespace(after=1)
        def commit(self, capture):
            pass
        def failure(self):
            pass
    for engine in engines.values():
        engine.register(view_reducer([Projection('board', (), lambda *_: (), lambda *_: {})]))
    owner = ContentOwner(engines, [], operational=[Operational(e) for e in engines.values()], lazy_sources=True)
    try:
        assert watched == []
        owner.demand(scopes[0], 'board', {})
        assert wait_until(lambda: len(watched) == 1)
        assert watched == [scopes[0]]
    finally:
        owner.close()
