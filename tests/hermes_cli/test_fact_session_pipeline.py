"""Real session content -> canonical assembly -> owner IPC -> parity slice.

This is not full route parity: selection, annotations and SQL count sources are
fixture-controlled here. It verifies composition without FakeStore or fake IPC.
"""
import json
import time

import pytest

from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_contract import Fact, FactKey, Scope, SourceId
from hermes_cli.fact_feed import ChangeFeed
from hermes_cli.fact_host import FactHost, acquire
from hermes_cli.fact_materializers import Projection, view_reducer
from hermes_cli.fact_parity import ExpectedView, compare_views
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_replica import Endpoint
from hermes_cli.fact_session_source import SessionContentSource
from hermes_cli.fact_store import FactStore, view_key
from hermes_cli.sessions_projection import assemble_sessions
from tests.hermes_cli import test_fact_session_source as helpers
from tests.waiting import wait_until

store = helpers.store


@pytest.mark.platforms('linux')
def test_native_session_content_to_canonical_view_over_real_ipc(store, tmp_path_factory):
    db, reader, path = store
    db.create_session('a', 'cli')
    scope = Scope('install', 'profile')
    key = FactKey(scope, 'SessionContentInput', 'a')
    facts = FactStore()
    engine = ReducerEngine(facts, scope)
    errors = []
    calls = []

    def render(selector, inputs, now):
        calls.append(inputs[key].value['input_tokens'])
        # Canonical function requires mutable detached rows (never proxy deepcopy).
        return assemble_sessions([dict(inputs[key].value)], {'profile': 1}, 20, 0, [], {})

    engine.register(view_reducer([Projection('sessions', ('SessionContentInput',),
                                             lambda *args: (key,), render)]))
    engine.bind(view_key(scope, 'sessions', {}))

    def commit(adapter, capture):
        engine.publish([Fact(FactKey(scope, 'SessionContentInput', row['id']), row, health='fresh')
                        for row in capture.records], now=time.monotonic())

    token = 'fixture-capability-token-only-00000000'
    host = FactHost(tmp_path_factory.mktemp('sp'), {token: scope}, store=facts).start()
    replica = acquire(scope, endpoint=Endpoint(str(host.directory / 'facts.sock'), token))
    feed = ChangeFeed(commit, lambda adapter, error: errors.append(error), budget=CaptureBudget(max_seconds=2))
    try:
        feed.register(SessionContentSource(SourceId(scope, 'sessions.content', 'local'), path, helpers.rows))
        assert wait_until(lambda: replica.v1.snapshot(scope).get(key) is not None, timeout=10)
        old = replica.v1.snapshot(scope)
        db._execute_write(lambda conn: conn.execute("UPDATE sessions SET input_tokens=37 WHERE id='a'"))
        assert wait_until(lambda: replica.v1.snapshot(scope).get(key).value['input_tokens'] == 37, timeout=10)
        snapshot = replica.v1.snapshot(scope)
        expected_row = next(iter(helpers.rows(reader, ('a',))))
        expected = assemble_sessions([expected_row], {'profile': 1}, 20, 0, [], {})
        assert compare_views(snapshot, [ExpectedView('sessions', {}, json.dumps(expected).encode())]).passed
        assert snapshot.get(key).rev == snapshot.get(view_key(scope, 'sessions', {})).rev
        assert old.get(key).value['input_tokens'] != 37
        before = len(calls)
        for _ in range(10):
            assert compare_views(snapshot, [ExpectedView('sessions', {}, json.dumps(expected).encode())]).passed
        assert len(calls) == before  # replica reads cannot call the owner projection
        assert not errors
    finally:
        feed.close()
        replica.close()
        host.close()
