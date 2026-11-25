"""Clock-only content bindings publish their cursor, never rows. [daveey]

Hub 2026-11-23: board/project-tree owners bound kanban.db (~66 MiB of task rows) and state.db (~78 MiB of session
rows) only for SourceState; the rows overflowed the shared 48 MiB store, every publication failed
fact_budget_exceeded and the feed rebuilt both sources every 5 s.
"""
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_content_owner import ContentBinding, ContentOwner
from hermes_cli.fact_contract import Fact, Scope, SourceId
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_source_commit import SourcePublisher
from hermes_cli.fact_store import FactStore
from hermes_cli.fact_view_owner import ViewOwner
from tests.hermes_cli import test_fact_session_source as sessions
from tests.hermes_cli import test_fact_task_source as tasks
from tests.waiting import wait_until

home, board = tasks.home, tasks.board
store = sessions.store
SCOPE = Scope("install", "profile", "board")


@pytest.fixture(autouse=True)
def _fast_clock_pacing(monkeypatch):
    """Clock-only sources pace their feed passes (5 s in production); a short pace keeps these waits short."""
    from hermes_cli import fact_content_owner
    monkeypatch.setattr(fact_content_owner, "CLOCK_ONLY_INTERVAL", 0.2)


@dataclass(frozen=True)
class _Cursor:
    n: int


def _capture(before, after, n_rows, pad):
    records = [{"id": str(i), "body": "x" * pad} for i in range(n_rows)]
    return SimpleNamespace(before=before, after=after, ids=[r["id"] for r in records], records=records,
                           full=True, observed_at=float(after.n))


def _publisher(store, clock_only):
    return SourcePublisher(ReducerEngine(store, SCOPE), SourceId(SCOPE, "tasks.content", "local"),
                           "TaskContentInput", clock_only=clock_only)


def test_clock_only_publishes_only_source_state_and_fits_where_rows_do_not():
    rows_mode = _publisher(FactStore(fact_bytes=256 * 1024), clock_only=False)
    with pytest.raises(ValueError, match="fact_budget_exceeded"):
        rows_mode.commit(_capture(None, _Cursor(1), 200, 4096))  # ~800 KiB of rows

    store = FactStore(fact_bytes=256 * 1024)
    clock = _publisher(store, clock_only=True)
    clock.commit(_capture(None, _Cursor(1), 200, 4096))
    snapshot = store.snapshot(SCOPE)
    assert set(store._facts) == {clock.state_key}
    first = snapshot.get(clock.state_key)
    assert first.value["cursor_state"] == {"n": 1} and first.health == "fresh"

    clock.commit(_capture(_Cursor(1), _Cursor(2), 200, 4096))
    second = store.snapshot(SCOPE).get(clock.state_key)
    assert second.value["cursor_state"] == {"n": 2} and second.rev > first.rev
    with pytest.raises(ValueError, match="cursor mismatch"):
        clock.commit(_capture(_Cursor(1), _Cursor(3), 1, 1))

    clock.failure()
    failed = store.snapshot(SCOPE)
    assert set(store._facts) == {clock.state_key}
    assert failed.get(clock.state_key).health == "stale"
    assert failed.get(clock.state_key).error_code == "source_unavailable"

    clock.commit(_capture(_Cursor(2), _Cursor(3), 200, 4096))  # recovery
    recovered = store.snapshot(SCOPE).get(clock.state_key)
    assert recovered.health == "fresh" and recovered.error_code is None
    assert recovered.value["cursor_state"] == {"n": 3} and set(store._facts) == {clock.state_key}


def test_clock_only_binding_rejects_a_custom_hydrate():
    scope = Scope("install", "profile")
    engine = ReducerEngine(FactStore(), scope)
    with pytest.raises(ValueError, match="clock-only"):
        ContentOwner({scope: engine}, [ContentBinding(SourceId(scope, "tasks.content", "b"), "/nonexistent",
                                                      hydrate=lambda conn, ids: (), clock_only=True)])


@pytest.mark.platforms("linux")
def test_clock_only_owner_advances_state_without_rows(board):
    first = tasks.kb.create_task(board, title="a", assignee="w")
    tasks.kb.create_task(board, title="b", assignee="w")
    path = next(r[2] for r in board.execute("PRAGMA database_list") if r[1] == "main")
    scope = Scope("install", "profile")
    source = SourceId(scope, "tasks.content", "board")
    store = FactStore()
    engine = ReducerEngine(store, scope)
    owner = ContentOwner({scope: engine}, [ContentBinding(source, path, clock_only=True)],
                         budget=CaptureBudget(max_seconds=2, max_keys=1))  # max_keys=1 forces paged bootstrap
    try:
        state_key = owner.publisher(source).state_key
        assert wait_until(lambda: store.snapshot(scope).get(state_key) is not None, timeout=10)
        before = store.snapshot(scope).get(state_key)
        with tasks.kbc.write_txn(board):
            board.execute("UPDATE tasks SET title='a2' WHERE id=?", (first,))
        assert wait_until(lambda: store.snapshot(scope).get(state_key).rev > before.rev, timeout=10)
        kinds = {key.kind for key in store._facts}
        assert kinds == {"SourceState"}, kinds
    finally:
        owner.close()


def test_view_owner_binds_content_sources_clock_only():
    scope = Scope("install", "profile", "board")
    binding = ContentBinding(SourceId(scope, "tasks.content", "local"), "/nonexistent/kanban.db")
    owner = ViewOwner(scope, lambda *args: None, lambda *args: None, ("board",), bindings=[binding])
    assert [b.clock_only for b in owner._bindings] == [True]
    assert owner._bindings[0].source == binding.source and owner._bindings[0].path == binding.path


@pytest.mark.platforms("linux")
def test_clock_only_session_owner_advances_state_without_rows(store, monkeypatch):
    db, _reader, path = store
    db.create_session("a", "cli")
    db.create_session("b", "cli")
    scope = Scope("install", "profile")
    source = SourceId(scope, "sessions.content", "local")
    facts = FactStore()
    owner = ContentOwner({scope: ReducerEngine(facts, scope)}, [ContentBinding(source, path, clock_only=True)],
                         budget=CaptureBudget(max_seconds=2, max_keys=1))  # max_keys=1 forces paged bootstrap
    try:
        state_key = owner.publisher(source).state_key
        assert wait_until(lambda: facts.snapshot(scope).get(state_key) is not None, timeout=10)
        before = facts.snapshot(scope).get(state_key)
        db._execute_write(lambda conn: conn.execute("UPDATE sessions SET input_tokens=37 WHERE id='a'"))
        assert wait_until(lambda: facts.snapshot(scope).get(state_key).rev > before.rev, timeout=10)
        create = db.create_session

        def observed_create(*args, **kwargs):
            prior = facts.snapshot(scope).get(state_key)
            result = create(*args, **kwargs)
            # Force the real feed to win before the writer returns. The old
            # post-write baseline then waits for a nonexistent second change.
            assert wait_until(lambda: facts.snapshot(scope).get(state_key).rev > prior.rev, timeout=10)
            return result

        monkeypatch.setattr(db, 'create_session', observed_create)
        mid = facts.snapshot(scope).get(state_key)
        db.create_session("c", "cli")
        assert wait_until(lambda: facts.snapshot(scope).get(state_key).rev > mid.rev, timeout=10)
        assert {key.kind for key in facts._facts} == {"SourceState"}
    finally:
        owner.close()


def test_ids_only_hydration_keeps_the_row_hydrator_contract():
    import sqlite3
    from hermes_cli.fact_content_owner import _ids_only
    conn = sqlite3.connect(":memory:", isolation_level=None)
    with pytest.raises(ValueError, match="transaction"):
        list(_ids_only(conn, ["a"]))
    conn.execute("BEGIN")
    assert list(_ids_only(conn, ["a", "b"])) == [{"id": "a"}, {"id": "b"}]
    with pytest.raises(ValueError, match="id batch"):
        _ids_only(conn, [1])
    with pytest.raises(ValueError, match="id batch"):
        _ids_only(conn, ["x"] * 501)
