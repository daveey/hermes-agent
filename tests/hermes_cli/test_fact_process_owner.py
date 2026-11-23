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


def test_both_consumer_apis_share_process_custody():
    from hermes_cli import fact_process
    from hermes_cli.fact_process_owner import ProcessOwner, process_store
    lease = fact_process.FeedLease(lambda *_: None, lambda *_: None)
    owner = ProcessOwner(lambda *_: None, lambda *_: None)
    try:
        assert owner._thread is lease._thread
        assert process_store() is fact_process.store()
        lease.close()
        assert owner._thread.is_alive()
    finally:
        lease.close()
        owner.close()


def test_close_retains_capture_custody_until_actual_completion(tmp_path):
    import pytest
    from hermes_cli.fact_contract import Scope
    from hermes_cli.fact_process import FeedLease, store
    from hermes_cli.fact_process_owner import ProcessOwner
    entered, release = threading.Event(), threading.Event()
    committed = []
    scope = Scope(str(tmp_path), 'blocked')
    storage = store()
    owner = ProcessOwner(lambda *args: committed.append(args), lambda *_: None,
                         stores={scope: storage})
    peer = FeedLease(lambda *_: None, lambda *_: None)
    class Blocking(Source):
        def capture(self, cursor, budget):
            entered.set()
            assert release.wait(5)
            return SimpleNamespace(after=1)
    source = Blocking([], ContextVar('blocking-profile', default='test'))
    owner.register(source)
    try:
        assert entered.wait(5)
        with pytest.raises(TimeoutError):
            owner.close(timeout=.01)
        assert not source.closed
        assert owner._thread.is_alive()
        with pytest.raises(RuntimeError, match='scope custody'):
            FeedLease(lambda *_: None, lambda *_: None, stores={scope: storage})
    finally:
        release.set()
        owner.close()
        peer.close()
    assert source.closed
    assert committed == []


def test_watch_close_failure_retains_scope_until_retry(tmp_path):
    import pytest
    from hermes_cli.fact_contract import Fact, FactKey, Scope
    from hermes_cli.fact_process import FeedLease, store
    from hermes_cli.fact_process_owner import ProcessOwner

    scope = Scope(str(tmp_path), 'watch-retry')
    storage = store()
    key = FactKey(scope, 'Input', 'retained')

    class FailedWatch(Source):
        def close(self):
            if not self.closed:
                self.closed = True
                raise TimeoutError('watch join failed')

    owner = FeedLease(lambda *_: None, lambda *_: None, stores={scope: storage})
    peer = ProcessOwner(lambda *_: None, lambda *_: None)
    source = FailedWatch([], ContextVar('watch-retry', default='test'))
    owner.register(source)
    storage.publish([Fact(key, 1)])
    try:
        with pytest.raises(TimeoutError, match='watch join failed'):
            owner.close()
        assert storage.snapshot(scope).get(key) is not None
        with pytest.raises(RuntimeError, match='scope custody'):
            ProcessOwner(lambda *_: None, lambda *_: None, stores={scope: storage})
        owner.close()
        assert storage.snapshot(scope).get(key) is None
        successor = ProcessOwner(lambda *_: None, lambda *_: None, stores={scope: storage})
        successor.close()
    finally:
        owner.close()
        peer.close()


def test_failed_final_feed_drain_refuses_new_tenure(monkeypatch):
    import pytest
    from hermes_cli.fact_process import FeedLease
    from hermes_cli.fact_process_owner import ProcessOwner

    owner = ProcessOwner(lambda *_: None, lambda *_: None)
    close = owner._feed.close

    def fail(timeout):
        raise TimeoutError('feed join failed')

    try:
        monkeypatch.setattr(owner._feed, 'close', fail)
        with pytest.raises(TimeoutError, match='feed join failed'):
            owner.close()
        with pytest.raises(RuntimeError, match='drain custody'):
            FeedLease(lambda *_: None, lambda *_: None)
    finally:
        monkeypatch.setattr(owner._feed, 'close', close)
        owner.close()
    successor = FeedLease(lambda *_: None, lambda *_: None)
    successor.close()


def test_last_close_retires_facts_without_stranding_inert_consumers(tmp_path):
    from hermes_cli.fact_contract import Fact, FactKey, Scope
    from hermes_cli.fact_process_owner import ProcessOwner, process_store
    first = process_store()
    scope = Scope(str(tmp_path), 'retired')
    key = FactKey(scope, 'Input', 'old')
    owner = ProcessOwner(lambda *_: None, lambda *_: None, stores={scope: first})
    first.publish([Fact(key, 'old tenure')])
    assert process_store() is first
    owner.close()
    second = process_store()
    assert second is first
    assert second.snapshot(scope).get(key) is None
    successor = ProcessOwner(lambda *_: None, lambda *_: None, stores={scope: second})
    try:
        second.publish([Fact(key, 'new tenure')])
        owner.close()
        assert first.snapshot(scope).get(key).value == 'new tenure'
    finally:
        successor.close()


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


def test_lazy_sources_watch_only_the_demanded_profile(tmp_path, monkeypatch):
    from hermes_cli.fact_board_inputs import CapturedInput
    from hermes_cli.fact_contract import Scope
    from hermes_cli.fact_view_owner import ViewOwner
    from hermes_cli import fact_board_inputs
    import time

    scopes = [Scope(str(tmp_path), f'profile-{i}') for i in range(11)]
    watched = []
    native_watch = fact_board_inputs.NativeSourceWatch

    def watch(paths, wake):
        watched.extend(paths)
        return native_watch(paths, wake)

    monkeypatch.setattr(fact_board_inputs, 'NativeSourceWatch', watch)
    owners = [ViewOwner(scope, lambda *_: CapturedInput({}, (), time.time()),
                        lambda *_: {}, ('board',), paths=[tmp_path / scope.profile_id / 'state.db'])
              for scope in scopes]
    try:
        assert watched == []
        owners[0].start().demand('board', {})
        assert wait_until(lambda: bool(watched))
        assert all(str(tmp_path / scopes[0].profile_id) in str(path) for path in watched)
        assert all(owner.owner is None for owner in owners[1:])
    finally:
        for owner in owners:
            owner.close()
