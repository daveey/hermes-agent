"""Canonical seven-family parity over the real scoped host, not FakeStore.

Only lifecycle authorization is test-only; production startup and release remain
coordinator-owned. Source inputs remain controlled canonical serializer fixtures.
"""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_annotation_facts as annotations
from hermes_cli.fact_contract import Fact, Scope
from hermes_cli.fact_host import FactHost, acquire
from hermes_cli.fact_replica import Endpoint
from tests.hermes_cli import test_kanban_annotation_facts as parity
from tests.hermes_cli.annotation_fact_processes import LifecycleFacade, request_guard
from tests.waiting import wait_until


class PublishedReplica:
    """Wait for actual IPC delivery before exercising a guarded request."""

    def __init__(self, host, replica, scope):
        self.host, self.replica, self.scope = host, replica, scope

    def publish(self, facts):
        self.host.store.publish(facts)
        expected = self.host.store.snapshot(self.scope)
        def delivered():
            snapshot = self.replica.snapshot(self.scope)
            return all(isinstance(snapshot.get(fact.key), Fact)
                       and snapshot.get(fact.key).value == expected.get(fact.key).value
                       for fact in facts)
        assert wait_until(delivered, timeout=10), 'real replica did not receive parity fact'


@pytest.fixture
def real_setup(tmp_path_factory, monkeypatch):
    scope = Scope('annotation-parity', 'default')
    directory = tmp_path_factory.mktemp('ap')
    token = 'p' * 48
    host = FactHost(directory, {token: scope})
    host.start()
    handle = acquire(scope, endpoint=Endpoint(str(directory / 'facts.sock'), token))
    facade = LifecycleFacade(handle.v1)
    consumer = annotations.AnnotationFacts(SimpleNamespace(v1=facade, demand=handle.demand), scope,
        stop_legacy=lambda: None, start_legacy=lambda: None)
    consumer.on_mode('facts', 1)  # TEST ONLY, never a production activation path
    monkeypatch.setattr(parity, 'no_io', lambda unused: request_guard())
    try:
        yield PublishedReplica(host, handle.v1, scope), scope, consumer, [], []
    finally:
        consumer.close()
        handle.close()
        host.close()


@pytest.mark.platforms('linux')
def test_real_host_kanban_parity(real_setup, monkeypatch):
    parity.test_real_kanban_serializer_parity_including_lineage_and_waits(real_setup, monkeypatch)


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('family,module,name', [
    ('goal', 'goal_session_activity', 'annotate_goal_activity'),
    ('cron', 'cron_session_activity', 'annotate_cron_activity'),
    ('background', 'process_session_activity', 'annotate_background_activity'),
    ('turn', 'turn_session_activity', 'annotate_turn_activity'),
    ('subagent', 'subagent_session_activity', 'annotate_subagent_activity'),
    ('review', 'review_session_activity', 'annotate_review_wait'),
])
def test_real_host_nonempty_family_parity(real_setup, monkeypatch, family, module, name):
    parity.test_nonempty_canonical_family_parity(real_setup, monkeypatch, family, module, name)
