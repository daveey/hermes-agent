"""Native project fact adapters retain the existing profile authorization wrapper.

This verifies no legacy data read inside that wrapper; its own scope lookup is
still an integration gate owned by the generic host scope binding.
"""
import json

import pytest

from hermes_cli import projects_fact_routes
from hermes_cli.fact_contract import Fact, Scope
from hermes_cli.fact_fake import FakeStore
from tui_gateway import server


@pytest.mark.parametrize("method,view,params,selector,payload", [
    ("projects.tree", "tree", {},
     {"surface": "native", "profile": None, "preview_limit": 3, "session_limit": 2000},
     {"projects": [], "active_id": None, "scoped_session_ids": []}),
    ("projects.project_sessions", "project_sessions", {"project_id": "p1"},
     {"surface": "native", "profile": None, "project_id": "p1", "session_limit": 5000},
     {"project": None}),
])
def test_native_fact_route_skips_legacy_database(method, view, params, selector, payload, monkeypatch):
    store = FakeStore()
    scope = Scope("install", "default")
    key = store.view_key(scope, view, selector)
    store.publish([Fact(key, {"encoded": json.dumps(payload).encode()}, health="fresh")])
    class Bound:
        def read(self, name, query):
            return store.snapshot(scope).view(name, query)
    monkeypatch.setattr(projects_fact_routes, "_installed", (store, lambda _: Bound()))
    def forbidden(*_args, **_kwargs):
        pytest.fail("native fact route entered legacy data reader")
    monkeypatch.setattr(server, "_profile_db", forbidden)
    monkeypatch.setattr(server, "_stamped_project_tree", forbidden)
    result = server._methods[method](1, params)
    assert result["result"] == payload
    store.publish(deleted_keys=[key])
    failure = server._methods[method](2, params)
    assert "error" in failure
    assert failure["error"]["code"] == 5061


@pytest.mark.parametrize("method,params", [
    ("projects.tree", {}), ("projects.project_sessions", {"project_id": "p1"}),
])
def test_native_actual_reader_drains_before_cutover(method, params, monkeypatch):
    import threading
    from contextlib import contextmanager
    from plugins.kanban.dashboard.board_fact_views import bind

    store = FakeStore()
    scope = Scope("install", "default")
    entered, finish, draining, stopped = (threading.Event() for _ in range(4))
    results, errors = [], []
    bound = bind(store, scope, stop_legacy=stopped.set, start_legacy=lambda: None,
                 demand=lambda *_: None, hot_selectors=[])
    monkeypatch.setattr(projects_fact_routes, "_installed", (store, lambda _: bound))

    @contextmanager
    def database(_params):
        # This boundary sits inside the real method's lease and auth wrapper.
        entered.set()
        assert finish.wait(5)
        yield object()

    monkeypatch.setattr(server, "_profile_db", database)
    monkeypatch.setattr(server, "_stamped_project_tree", lambda *a, **k: (
        {"projects": [{"id": "p1"}], "scoped_session_ids": []}, None))
    wait_for = bound._readers.wait_for

    def observed_wait(predicate, timeout):
        draining.set()
        return wait_for(predicate, timeout)

    monkeypatch.setattr(bound._readers, "wait_for", observed_wait)

    def activate():
        try:
            bound.on_mode("facts", 1)
        except (RuntimeError, TimeoutError) as exc:
            errors.append(exc)

    reader = threading.Thread(target=lambda: results.append(server._methods[method](1, params)))
    transition = threading.Thread(target=activate)
    try:
        reader.start()
        assert entered.wait(5)
        assert bound._active == 1
        transition.start()
        assert draining.wait(5)
        assert not stopped.is_set()
        finish.set()
        reader.join(5)
        transition.join(5)
        assert not reader.is_alive() and not transition.is_alive()
        assert not errors and stopped.is_set() and bound._active == 0
        assert "result" in results[0]
    finally:
        finish.set()
        reader.join(5)
        if transition.ident is not None:
            transition.join(5)
        bound.close()
