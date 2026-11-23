"""Existing board route parity against in-process, independently released facts.

Real local data/release planes; controlled canonical captures, not production
source materialization or authorized startup proof.
"""
import json

import pytest

from hermes_cli import projects_fact_routes
from hermes_cli.fact_contract import Fact, Scope
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle
from hermes_cli.fact_store import FactStore, view_key
from plugins.kanban.dashboard import board_fact_views as views
from tests.plugins import test_kanban_board_replica as parity

api = parity.api


class BoardControl:
    def __init__(self, life):
        self.life, self.generation = life, 0

    def activate(self, scope):
        self.generation += 1
        self.life.transition('board', scope, 'facts', self.generation)

    def rollback(self, scope):
        self.generation += 1
        self.life.transition('board', scope, 'legacy', self.generation)


@pytest.fixture
def local(monkeypatch):
    scope = Scope('install', 'profile', 'default')
    store = FactStore()
    life = IncrementalLifecycle({(scope, 'board')})
    owner = InProcessFacts(store, {scope}, demand=lambda *args: None)
    handle = owner.acquire(scope, lifecycle=life)
    transitions = []
    bound = views.bind(handle.v1, scope, stop_legacy=lambda: transitions.append('stop'),
                       start_legacy=lambda: transitions.append('start'), demand=handle.demand,
                       hot_selectors=[('board', views.board_selector())])
    monkeypatch.setattr(views, '_installed', (handle.v1, lambda _: bound))
    monkeypatch.setattr(projects_fact_routes, '_installed', (handle.v1, lambda _: bound))
    control = BoardControl(life)

    def publish(name, selector, payload=None, *, health='fresh', delete=False):
        key = view_key(scope, name, selector)
        if delete:
            store.publish(deleted_keys=[key])
        else:
            store.publish([Fact(key, {'encoded': json.dumps(payload).encode()}, health=health,
                                error_code='source_missing' if health != 'fresh' else None)])
        return key

    try:
        yield scope, handle, bound, control, transitions, publish
    finally:
        control.rollback(scope)
        bound.close()
        handle.close()
        owner.close()


def test_local_all_exported_views_and_rollback(api, local, monkeypatch):
    parity.test_real_replica_all_exported_views_and_rollback(api, local, monkeypatch)


def test_local_canonical_board_graph_delete(api, local, monkeypatch):
    parity.test_real_replica_frozen_canonical_board_graph_delete(api, local, monkeypatch)


def test_local_delta_health_delete_and_filters(api, local):
    parity.test_real_replica_delta_health_delete_and_filters(api, local)
