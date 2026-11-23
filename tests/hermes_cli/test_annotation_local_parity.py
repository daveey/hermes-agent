"""Canonical annotation fixtures through real local transport/release control.

Source input fixtures are controlled; this does not certify startup authorization
or automatic native-source materialization. No FakeStore or fake latch is used.
"""
import pytest

from hermes_cli import kanban_annotation_facts as annotations
from hermes_cli.fact_contract import Scope
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle
from hermes_cli.fact_store import FactStore
from tests.hermes_cli import test_kanban_annotation_facts as parity
from tests.hermes_cli.annotation_fact_processes import request_guard


@pytest.fixture
def local_setup(monkeypatch):
    scope = Scope('local-parity', 'default')
    store = FactStore()
    life = IncrementalLifecycle({(scope, 'annotations')})
    owner = InProcessFacts(store, {scope}, demand=lambda *args: None)
    handle = owner.acquire(scope, lifecycle=life)
    consumer = annotations.AnnotationFacts(handle, scope, stop_legacy=lambda: None, start_legacy=lambda: None)
    # This test explicitly authorizes its controlled fixture; no automatic
    # readiness or production startup authorization is inferred from publication.
    life.report_ready('annotations', scope)
    life.transition('annotations', scope, 'facts', 1)
    monkeypatch.setattr(parity, 'no_io', lambda unused: request_guard())
    try:
        yield store, scope, consumer, [], []
    finally:
        life.transition('annotations', scope, 'legacy', 2)
        consumer.close()
        handle.close()
        owner.close()


def test_local_kanban_parity(local_setup, monkeypatch):
    parity.test_real_kanban_serializer_parity_including_lineage_and_waits(local_setup, monkeypatch)


@pytest.mark.parametrize('family,module,name', [
    ('goal', 'goal_session_activity', 'annotate_goal_activity'),
    ('cron', 'cron_session_activity', 'annotate_cron_activity'),
    ('background', 'process_session_activity', 'annotate_background_activity'),
    ('turn', 'turn_session_activity', 'annotate_turn_activity'),
    ('subagent', 'subagent_session_activity', 'annotate_subagent_activity'),
    ('review', 'review_session_activity', 'annotate_review_wait'),
])
def test_local_nonempty_family_parity(local_setup, monkeypatch, family, module, name):
    parity.test_nonempty_canonical_family_parity(local_setup, monkeypatch, family, module, name)
