"""Composed native DB → existing content owner → pure board ViewFact. [daveey]

This exercises real sources and mutation hints, not serve startup activation.
"""
from contextlib import closing
import json
import time

from hermes_cli import kanban_db, kanban_db_connect
from hermes_cli.fact_board_inputs import BoardInputSource, CapturedInput
from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_content_owner import ContentBinding, ContentOwner
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_materializers import Projection, view_reducer
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore, view_key
from plugins.kanban.dashboard import board_fact_views
from hermes_cli import fact_board_native as board_native_inputs
from tests.plugins import test_kanban_board_shadow as helpers
from tests.waiting import wait_until

shadow_board = helpers.shadow_board


def test_real_source_mutation_reaches_canonical_view_without_legacy_route(shadow_board, monkeypatch):
    monkeypatch.setattr(time, 'time', lambda: 1_700_000_000.0)
    scope = Scope('fixture-install', 'default', 'default')
    engine = ReducerEngine(FactStore(), scope)
    source_id = SourceId(scope, 'board.input', 'local')
    content_id = SourceId(scope, 'tasks.content', 'local')
    path = kanban_db.kanban_db_path()
    api = shadow_board['mod']

    def capture(view, selector, budget):
        result = board_native_inputs.capture(api, None, view, selector)
        return CapturedInput(result['value'], ((source_id, result['observations']),), result['observed_at'])

    source = BoardInputSource(engine, source_id, capture, paths=(path,))

    def dependencies(scope, selector, snapshot):
        return {source.input_key('board', selector), owner.publisher(content_id).state_key}

    def render(selector, facts, now):
        return board_native_inputs.project(api, 'board', facts[source.input_key('board', selector)].value)

    engine.register(view_reducer([Projection('board', ('BoardInput', 'SourceState'), dependencies, render)]))
    owner = ContentOwner({scope: engine}, [ContentBinding(content_id, path)], operational=[source],
                         budget=CaptureBudget(max_seconds=5))
    selector = board_fact_views.board_selector()
    key = view_key(scope, 'board', selector)

    def payload():
        fact = engine.store.snapshot(scope).get(key)
        return json.loads(fact.value['encoded']) if fact is not None and fact.health == 'fresh' else None

    try:
        source.want('board', selector)
        owner.demand(scope, 'board', selector)
        assert wait_until(lambda: payload() is not None, timeout=5)
        assert payload() == helpers._read(shadow_board)
        with closing(kanban_db_connect.connect()) as conn, conn:
            conn.execute('UPDATE tasks SET title = ? WHERE id = ?', ('Native mutation', shadow_board['live_id']))
        assert wait_until(lambda: payload() is not None and
                          helpers._cards(payload())[shadow_board['live_id']]['title'] == 'Native mutation', timeout=5)
        fact = engine.store.snapshot(scope).get(key)
        assert any(entry[0] == content_id for entry in fact.source_vector)
        assert any(entry[0] == source_id for entry in fact.source_vector)
        assert payload() == helpers._read(shadow_board)
    finally:
        owner.close()
