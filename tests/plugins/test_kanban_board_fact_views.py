"""Board consumer contract using the same executable store as the producer."""
import json

import pytest

from hermes_cli.fact_contract import Fact, Scope
from hermes_cli.fact_fake import FakeStore
from plugins.kanban.dashboard.board_fact_views import bind, board_selector


@pytest.fixture
def consumer():
    store = FakeStore()
    scope = Scope("install", "profile", "board")
    calls = []
    demands = []
    selector = board_selector()
    bound = bind(store, scope, stop_legacy=lambda: calls.append("stop"),
                 start_legacy=lambda: calls.append("start"),
                 demand=lambda *args: demands.append(args), hot_selectors=[("board", selector)])
    for name in ("sessions", "panel", "annotations", "notifications"):
        store.bind_consumer(name, scope, lambda *_: None)
        store.report_ready(name, scope)
    yield store, scope, bound, calls, demands
    bound.close()


def publish(store, scope, payload, *, health="fresh", selector=None):
    selector = board_selector() if selector is None else selector
    fact = Fact(store.view_key(scope, "board", selector),
                {"view": "board", "selector": selector, "payload": payload,
                 "encoded": json.dumps(payload).encode()},
                observed_at=1000, health=health)
    store.publish([fact])
    return fact


def activate(consumer, payload):
    store, scope, bound, _, _ = consumer
    publish(store, scope, payload)
    assert bound.prepare()
    store.activate(scope)
    store.drain()


def test_latch_requires_prewarm_and_stops_legacy_once(consumer):
    store, scope, bound, calls, demands = consumer
    assert bound.read("board", board_selector()) is None
    assert calls == ["start"]
    assert not bound.prepare()
    assert demands == [("board", board_selector())]
    with pytest.raises(RuntimeError, match="not ready"):
        store.activate(scope)
    activate(consumer, {"columns": []})
    store.activate(scope)
    assert calls == ["start", "stop"]
    store.rollback(scope)
    store.rollback(scope)
    assert calls == ["start", "stop", "start"]
    assert bound.read("board", board_selector()) is None


@pytest.mark.parametrize("payload", [{"columns": []}, {"columns": [{"name": "ready", "tasks": []}]}])
def test_empty_ready_is_not_pending_or_unknown(consumer, payload):
    store, scope, bound, _, demands = consumer
    activate(consumer, payload)
    assert json.loads(bound.read("board", board_selector()).encoded) == payload
    publish(store, scope, payload, health="unknown")
    result = bound.read("board", board_selector())
    assert isinstance(result, store.Unavailable)
    assert demands == []
    publish(store, scope, payload, health="stale")
    assert isinstance(bound.read("board", board_selector()), store.Unavailable)


def test_tombstone_and_new_selector_never_fall_back(consumer):
    store, scope, bound, calls, demands = consumer
    activate(consumer, {"columns": []})
    key = store.view_key(scope, "board", board_selector())
    store.publish(deleted_keys=[key])
    assert isinstance(bound.read("board", board_selector()), store.Pending)
    other = board_selector(tenant="elsewhere", done_limit=0)
    assert isinstance(bound.read("board", other), store.Pending)
    assert demands == [("board", board_selector()), ("board", other)]
    assert calls == ["start", "stop"]


def test_scope_cannot_read_another_profiles_view(consumer):
    store, scope, bound, _, _ = consumer
    activate(consumer, {"columns": []})
    publish(store, Scope(scope.install_id, "foreign", scope.board_id), {"secret": True})
    assert json.loads(bound.read("board", board_selector()).encoded) == {"columns": []}


def test_delta_reads_latest_generation_without_waiting_for_callback(consumer):
    store, scope, bound, _, _ = consumer
    activate(consumer, {"columns": [{"name": "ready", "tasks": [{"id": "a"}]}]})
    previous = bound.read("board", board_selector())
    payload = {"columns": [{"name": "ready", "tasks": []}]}
    publish(store, scope, payload)
    current = bound.read("board", board_selector())
    assert current.rev > previous.rev
    assert json.loads(current.encoded) == payload
    assert json.loads(previous.encoded)["columns"][0]["tasks"] == [{"id": "a"}]


def test_stale_lifecycle_generation_does_not_restart_readers(consumer):
    _, _, bound, calls, _ = consumer
    activate(consumer, {"columns": []})
    bound.on_mode("legacy", 0)
    assert calls == ["start", "stop"]
    with pytest.raises(ValueError, match="unsupported"):
        bound.on_mode("surprise", 2)


def test_close_is_idempotent_and_never_starts_legacy(consumer):
    store, _, bound, calls, _ = consumer
    activate(consumer, {"columns": []})
    bound.close()
    bound.close()
    assert calls == ["start", "stop"]
    assert isinstance(bound.read("board", board_selector()), store.Unavailable)


def test_failed_drain_remains_unavailable_until_coordinated_rollback(consumer):
    store, _, bound, calls, _ = consumer
    def fail():
        raise RuntimeError("reader did not drain")
    original_stop = bound._stop_legacy
    bound._stop_legacy = fail
    with pytest.raises(RuntimeError, match="did not drain"):
        bound.on_mode("facts", 1)
    assert isinstance(bound.read("board", board_selector()), store.Unavailable)
    bound.on_mode("legacy", 1)
    assert calls == ["start", "start"]
    bound._stop_legacy = original_stop


def test_failed_transition_rejects_older_rollback(consumer):
    store, _, bound, calls, _ = consumer
    original_stop = bound._stop_legacy

    def fail():
        raise RuntimeError("not drained")

    bound._stop_legacy = fail
    try:
        with pytest.raises(RuntimeError, match="not drained"):
            bound.on_mode("facts", 2)
        bound.on_mode("legacy", 1)
        assert calls == ["start"]
        assert isinstance(bound.read("board", board_selector()), store.Unavailable)
        bound.on_mode("legacy", 2)
        assert calls == ["start", "start"]
    finally:
        bound._stop_legacy = original_stop


def test_pending_read_cannot_demand_after_close(consumer, monkeypatch):
    import threading
    from types import SimpleNamespace

    store, _scope, bound, _calls, demands = consumer
    activate(consumer, {"columns": []})
    entered, finish = threading.Event(), threading.Event()
    results = []

    def view(*args):
        entered.set()
        assert finish.wait(5)
        return store.Pending("cold")

    monkeypatch.setattr(store, "snapshot", lambda _: SimpleNamespace(view=view))
    thread = threading.Thread(target=lambda: results.append(bound.read("board", board_selector())))
    try:
        thread.start()
        assert entered.wait(5)
        bound.close()
        finish.set()
        thread.join(5)
        assert not thread.is_alive()
        assert demands == []
        assert isinstance(results[0], store.Unavailable)
    finally:
        finish.set()
        thread.join(5)


def test_selector_retains_all_filter_dimensions():
    assert board_selector("tenant", True, "workflow", "step", 0) == {
        "tenant": "tenant", "include_archived": True,
        "workflow_template_id": "workflow", "current_step_key": "step", "done_limit": 0}


def test_legacy_readiness_follows_publication_without_polling(consumer):
    store, scope, bound, calls, _ = consumer
    assert not bound.prepare()
    publish(store, scope, {"columns": []})
    store.drain()
    # No second explicit prepare; scoped ViewFact publication supplies readiness.
    store.activate(scope)
    assert calls == ["start", "stop"]
    store.rollback(scope)
    publish(store, scope, {"columns": []}, health="unknown")
    store.drain()
    with pytest.raises(RuntimeError, match="not ready"):
        store.activate(scope)
    assert calls == ["start", "stop", "start"]


def test_prepare_after_close_cannot_recreate_readiness_or_demand(consumer, monkeypatch):
    store, _scope, bound, _calls, demands = consumer
    bound.close()

    def forbidden(*args, **kwargs):
        pytest.fail("closed consumer touched snapshot/readiness")

    monkeypatch.setattr(store, "snapshot", forbidden)
    monkeypatch.setattr(store, "report_ready", forbidden)
    assert bound.prepare() is False
    bound._changed(None)  # an already queued callback is harmless too
    assert demands == []


def test_close_from_legacy_stops_reader_once(consumer):
    _store, _scope, bound, calls, _demands = consumer
    bound.close()
    bound.close()
    assert calls == ["start", "stop"]
