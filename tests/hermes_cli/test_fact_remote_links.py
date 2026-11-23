"""Actual origin ledger capture through shared owner and display call boundary."""
import threading

import pytest

from hermes_cli import kanban_session_activity as activity
from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_content_owner import ContentOwner
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore
from tests.hermes_cli import test_kanban_origin_ledger as ledger_tests
from tests.waiting import wait_until

world = ledger_tests.world


def test_native_ledger_to_display_without_request_capture(world, tmp_path, monkeypatch):
    from hermes_cli.fact_remote_links import RemoteLinksSource, LinksFacts, use, LinksUnavailable
    from hermes_cli import kanban_origin_adapter as adapter
    from hermes_cli import profiles
    import hermes_constants

    path, replica, binding = world
    home = tmp_path / 'home'
    (home / 'kanban').mkdir(parents=True)
    path = home / 'kanban' / 'origin-ledger.db'
    world = path, replica, binding
    ledger_tests.publish(replica, binding)
    assert ledger_tests.accept(world)['status'] == 'accepted'
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: home)
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(adapter, '_gateway', lambda _: 'origin')
    monkeypatch.setattr(activity, '_hub_db_path', lambda: replica)
    # No historical legacy association inventory in this fixture; receipts are real.
    monkeypatch.setattr(activity, '_legacy_remote_links', list)
    monkeypatch.setattr(activity, '_without_local_open_duplicates', lambda rows, **_: rows)
    scope = Scope('install', 'default')
    facts = FactStore()
    engine = ReducerEngine(facts, scope)
    source = RemoteLinksSource(engine, SourceId(scope, 'remote.links', 'origin'), activity._origin_scope())
    owner = ContentOwner({scope: engine}, [], operational=[source], budget=CaptureBudget(max_seconds=2))
    local = InProcessFacts(facts, {scope}, demand=owner.demand)
    handle = local.acquire(scope, lifecycle=IncrementalLifecycle({(scope, 'links')}))
    try:
        assert wait_until(lambda: facts.snapshot(scope).get(source.key) is not None, timeout=10)
        captured = facts.snapshot(scope).get(source.key)
        assert captured.health == 'fresh'
        assert captured.value['cards'][0]['unfinished'] is True
        before = facts.snapshot(scope)
        from copy import deepcopy
        second = deepcopy(binding)
        second['binding_id'] = 'binding-0002'
        second['owner']['task_id'] = 't_two'
        second['task_record_key'] = 'work/build/t_two'
        ledger_tests.publish(replica, second)
        assert ledger_tests.accept((path, replica, second), request_id='request-0002')['status'] == 'accepted'
        # Exercise the scheduled recapture entry without a 30-second test sleep.
        source._wake()
        assert wait_until(lambda: len(facts.snapshot(scope).get(source.key).value['cards']) == 2, timeout=10)
        assert len(before.get(source.key).value['cards']) == 1
        bridge = LinksFacts(handle, scope, source._origin_scope)
        from hermes_cli import kanban_session_activity_revalidate as revalidate
        monkeypatch.setattr(revalidate.force_read, 'scope', source._origin_scope, raising=False)
        with use(bridge), pytest.raises(LinksUnavailable, match='forced'):
            activity._remote_links(stale_ok=True)
        monkeypatch.setattr(revalidate.force_read, 'scope', None)
        expected = activity._project_remote_links()
        def forbidden(*args, **kwargs):
            pytest.fail('display request attempted legacy capture or refresher')
        monkeypatch.setattr(activity, '_origin_scope', forbidden)
        monkeypatch.setattr(activity, '_links_lookup', forbidden)
        monkeypatch.setattr(activity, '_refresh_links_in_background', forbidden)
        with use(bridge):
            assert activity._remote_links(stale_ok=True) == expected
            cards, rows = activity.remote_links_and_origin_rows(stale_ok=True)
            assert cards == expected and rows[0]['key'] == cards[0]['key']
            cards[0]['status'] = 'caller mutation'
            assert activity._remote_links(stale_ok=True) == expected
        assert captured.value['cards'][0]['status'] != 'caller mutation'
        source.failure()
        with use(bridge), pytest.raises(LinksUnavailable):
            activity._remote_links(stale_ok=True)
    finally:
        owner.close()
        handle.close()
        local.close()


def test_slow_capture_does_not_block_feed_and_close_retains_custody(monkeypatch):
    from hermes_cli.fact_remote_links import RemoteLinksSource
    scope = Scope('install', 'default')
    engine = ReducerEngine(FactStore(), scope)
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(activity, '_origin_scope', lambda: ('authorized',))
    def slow():
        started.set()
        assert release.wait(5)
        return []
    monkeypatch.setattr(activity, '_project_remote_links', slow)
    source = RemoteLinksSource(engine, SourceId(scope, 'remote.links', 'origin'), ('authorized',))
    woke = threading.Event()
    source.watch(woke.set)
    try:
        pending = source.capture(None, CaptureBudget(max_seconds=2))
        assert pending.value is None and pending.next_deadline is not None
        assert started.wait(5)
        source._deadline = 0  # deterministic deadline, worker still blocked
        timed_out = source.capture(None, CaptureBudget(max_seconds=2))
        source.commit(timed_out)
        assert engine.store.snapshot(scope).get(source.key).health == 'unknown'
        assert source.capture(None, CaptureBudget(max_seconds=2)).next_deadline is None
        with pytest.raises(TimeoutError):
            source.close(timeout=0)
        release.set()
        source.close()
        assert engine.store.snapshot(scope).get(source.key).health == 'unknown'
    finally:
        release.set()
        source.close()
