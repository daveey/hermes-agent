"""Canonical projection -> published ViewFact integration. [daveey]"""

from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore, view_key


def test_failed_serializer_is_unavailable_without_blocking_sibling_and_recovers():
    from hermes_cli.fact_materializers import Projection, view_reducer

    scope = Scope("i", "p")
    source = FactKey(scope, "TaskFact", "1")
    store = FactStore()
    engine = ReducerEngine(store, scope)

    def render(selector, facts, now):
        value = facts[source].value["value"]
        if value == "bad":
            raise RuntimeError("private diagnostic must not escape")
        if value == "invalid":
            return {"value": float("nan")}
        return {"value": value}

    engine.register(view_reducer([
        Projection("board", ("TaskFact",), lambda *args: (source,), render),
        Projection("boards", ("TaskFact",), lambda *args: (source,), lambda *args: {"ok": True}),
    ]))
    for name in ("board", "boards"):
        engine.bind(view_key(scope, name, {}))
    for value in ("good", "bad", "invalid", "recovered"):
        engine.publish([Fact(source, {"value": value}, health="fresh")], now=10)
        snapshot = store.snapshot(scope)
        assert snapshot.view("boards", {}).encoded == b'{"ok":true}'
        fact = snapshot.get(view_key(scope, "board", {}))
        assert snapshot.get(source).value["value"] == value
        if value in ("bad", "invalid"):
            assert fact.health == "unknown"
            assert fact.error_code == "projection_failed"
            assert "encoded" not in fact.value
            assert "private" not in repr(fact)
        else:
            assert fact.health == "fresh"
            assert fact.value["payload"]["value"] == value


def test_unknown_view_demand_is_refused_before_it_can_poison_publication():
    import pytest
    from hermes_cli.fact_materializers import Projection, view_reducer

    scope = Scope("i", "p")
    engine = ReducerEngine(FactStore(), scope)
    engine.register(view_reducer([Projection("board", (), lambda *args: (), lambda *args: {})]))
    with pytest.raises(ValueError, match="unregistered projection"):
        engine.bind(view_key(scope, "missing", {}))
    assert engine.publish([Fact(FactKey(scope, "SourceState", "s"), {})], now=1) == 1


def test_canonical_goal_serializer_to_view_matches_legacy_entrypoint(monkeypatch):
    import json
    from hermes_cli import goal_session_activity as goal
    from hermes_cli.fact_materializers import Projection, view_reducer
    from hermes_cli.fact_parity import ExpectedView, compare_views

    scope = Scope("i", "p")
    session = FactKey(scope, "SessionFact", "s1")
    inputs = FactKey(scope, "GoalInput", "s1")
    row = {"id": "s1", "_lineage_ids": ["ancestor"]}
    monkeypatch.setattr(goal, "active_goal_sessions", lambda: frozenset({"ancestor"}))
    legacy = goal.annotate_goal_activity([dict(row)])

    def no_capture():
        raise AssertionError("materializer called legacy capture")

    monkeypatch.setattr(goal, "active_goal_sessions", no_capture)

    def canonical(selector, facts, now):
        detached = dict(facts[session].value["compact"])
        return goal.project_goal_activity([detached], facts[inputs].value["active"])

    store = FactStore()
    engine = ReducerEngine(store, scope)
    engine.register(view_reducer([Projection("annotations", ("SessionFact", "GoalInput"),
                                             lambda *args: (session, inputs), canonical)]))
    engine.bind(view_key(scope, "annotations", {"session_id": "s1"}))
    engine.publish([Fact(session, {"compact": row}, health="fresh"),
                    Fact(inputs, {"active": ["ancestor"]}, health="fresh")], now=10)
    report = compare_views(store.snapshot(scope), [ExpectedView("annotations", {"session_id": "s1"},
                                                               json.dumps(legacy).encode())])
    assert report.passed


def test_projection_materializes_atomically_with_dependency_health():
    from hermes_cli.fact_materializers import Projection, view_reducer

    scope = Scope("i", "p")
    task = FactKey(scope, "TaskFact", "local:1")
    view = view_key(scope, "board", {"board": "default"})
    store = FactStore()
    engine = ReducerEngine(store, scope)
    calls = []

    def canonical(selector, facts, now):
        calls.append(selector)
        return {"tasks": [dict(facts[task].value["card"])], "clock": now}

    engine.register(view_reducer([Projection("board", ("TaskFact",), lambda scope, selector, snapshot: (task,), canonical)]))
    engine.bind(view)
    engine.publish([Fact(task, {"card": {"id": "1", "status": "ready"}}, health="fresh", valid_until=20)], now=10)
    snapshot = store.snapshot(scope)
    assert snapshot.view("board", {"board": "default"}).encoded == b'{"tasks":[{"id":"1","status":"ready"}],"clock":10}'
    assert snapshot.get(view).rev == snapshot.get(task).rev
    assert snapshot.get(view).valid_until == 20
    assert len(calls) == 1
    engine.publish([Fact(task, {"card": {"id": "1", "status": "ready"}}, health="stale")], now=11)
    assert store.snapshot(scope).get(view).health == "stale"
    assert len(calls) == 1  # Never run a canonical reducer on unavailable inputs.
    engine.publish(deleted_keys=[task], now=12)
    assert store.snapshot(scope).get(view).health == "unknown"
    assert len(calls) == 1
