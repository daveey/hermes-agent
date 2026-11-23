"""Health transition wakeups, owner isolation and same-generation acknowledgement."""
import time
from unittest.mock import Mock

import pytest

from hermes_cli import fact_links_health as health
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_remote_links import LinksCapture, RemoteLinksSource
from hermes_cli.fact_store import FactStore


@pytest.fixture(autouse=True)
def isolated_health():
    health.reset_for_tests()
    yield
    health.reset_for_tests()


def test_source_publication_notifies_health_after_actual_fact_commit():
    scope = Scope('home', 'default')
    origin = ('home', 'default', 'boards', 'hub', 'mail')
    engine = ReducerEngine(FactStore(), scope)
    source = RemoteLinksSource(engine, SourceId(scope, 'remote.links', 'origin'), origin)
    observations = []
    watch = health.HealthWatch(lambda: observations.append(engine.store.snapshot(scope).get(source.key).health))
    try:
        capture = LinksCapture(value={'cards': [], 'origin_rows': []}, observed_at=time.time())
        source.commit(capture)
        assert observations == ['fresh']
        source.commit(capture)  # unchanged health does not wake on every recapture
        assert observations == ['fresh']
        source.failure()  # preserve the old no-argument contract
        assert observations == ['fresh', 'stale']
        source.failure('links_capture_time_budget')
        assert observations == ['fresh', 'stale']
        source.commit(capture)
        assert observations == ['fresh', 'stale', 'fresh']
    finally:
        watch.close()


def test_watch_scope_and_ack_do_not_consume_newer_failure_or_another_owner():
    wake = Mock()
    watch = health.HealthWatch(wake)
    a = ('home', 'default', 'boards', 'hub', 'mail')
    b = ('other', 'profile', 'boards', 'hub', 'mail')
    try:
        health.changed(b, False)
        assert not watch.due({'home'})
        assert health.pending(a) is None
        health.changed(a, False)
        failed = health.pending(a)
        assert watch.due({'home'})
        assert not watch.due({'home'})
        health.changed(a, True)
        recovered = health.pending(a)
        health.acknowledge(a, failed)
        assert health.pending(a) == recovered
        health.changed(a, False)  # failure while recovery work was in flight
        health.acknowledge(a, recovered)
        assert health.pending(a) not in (failed, recovered)
        assert health.pending(b) is not None
    finally:
        watch.close()
    calls = wake.call_count
    health.changed(a, True)
    assert wake.call_count == calls
