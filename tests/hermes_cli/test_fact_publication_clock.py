"""Publication timestamps use epoch time; scheduler deadlines stay monotonic."""
from types import SimpleNamespace

import pytest

from hermes_cli.fact_contract import Fact, FactKey, Scope, SourceId
from hermes_cli.fact_demand_source import DemandSource
from hermes_cli.fact_materializers import Projection, view_reducer
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_source_commit import SourcePublisher
from hermes_cli.fact_store import FactStore, view_key
from tests.hermes_cli.test_fact_source_commit import capture
from tests.hermes_cli import test_fact_task_source as tasks

home, board = tasks.home, tasks.board


@pytest.mark.parametrize('path', ['demand', 'source', 'failure'])
def test_publication_uses_wall_clock_for_projection_expiry(monkeypatch, path, board):
    from hermes_cli import fact_demand_source, fact_source_commit
    wall, monotonic = 1_800_000_000.0, 100.0
    clock = SimpleNamespace(time=lambda: wall, monotonic=lambda: monotonic)
    monkeypatch.setattr(fact_demand_source, 'time', clock)
    monkeypatch.setattr(fact_source_commit, 'time', clock)
    scope = Scope('install', 'profile')
    engine = ReducerEngine(FactStore(), scope)
    source = SourceId(scope, 'tasks.content', 'board')
    publisher = SourcePublisher(engine, source, 'TaskContentInput')
    expired = FactKey(scope, 'ExternalInput', 'expired')
    engine.publish([Fact(expired, {}, health='fresh', valid_until=wall - 1)], now=wall)
    seen = []
    engine.register(view_reducer([
        Projection('expired', ('ExternalInput',), lambda *args: (expired,),
                   lambda *args: {'incorrectly_fresh': True}),
        Projection('clock', (), lambda *args: (),
                   lambda selector, facts, now: seen.append(now) or {'now': now}),
    ]))
    mailbox = DemandSource(engine)
    mailbox.watch(lambda: None)
    try:
        if path == 'demand':
            for name in ('clock', 'expired'):
                mailbox.demand(name, {})
                mailbox.commit(mailbox.capture(None, None))
        else:
            for name in ('clock', 'expired'):
                engine.bind(view_key(scope, name, {}))
            if path == 'failure':
                publisher.failure()
            else:
                tasks.kb.create_task(board, title='native', assignee='w')
                db_path = next(row[2] for row in board.execute('PRAGMA database_list') if row[1] == 'main')
                publisher.commit(capture(db_path))
        snapshot = engine.store.snapshot(scope)
        # Feed scheduler deadlines deliberately remain on the other clock.
        mailbox.demand('clock', {'barrier': 1})
        mailbox.demand('clock', {'barrier': 2})
        assert mailbox.capture(None, None).next_deadline == monotonic
        assert seen == [wall]
        assert snapshot.get(view_key(scope, 'clock', {})).observed_at == wall
        assert snapshot.get(view_key(scope, 'expired', {})).health == 'stale'
    finally:
        mailbox.close()
