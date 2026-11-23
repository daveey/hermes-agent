"""Project tree fact reads bypass both direct and HTTP legacy caches."""
import asyncio
import builtins
import json
import socket
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hermes_cli import projects_fact_routes
from hermes_cli.fact_contract import Fact, Scope
from hermes_cli.fact_fake import FakeStore
from hermes_cli.web_routers import profiles


@pytest.fixture
def installed(monkeypatch):
    store = FakeStore()
    scope = Scope("install", "profile")
    selector = {"surface": "profiles", "preview_limit": 3, "session_limit": 2000, "profile": None}
    payload = {"projects": [], "active_id": None, "scoped_session_ids": [], "errors": []}
    key = store.view_key(scope, "tree", selector)
    store.publish([Fact(key, {"encoded": json.dumps(payload).encode()}, health="fresh")])
    class Bound:
        def read(self, view, query):
            return store.snapshot(scope).view(view, query)
    monkeypatch.setattr(projects_fact_routes, "_installed", (store, lambda _: Bound()))
    return store, key, payload


def test_exported_tree_direct_and_http_do_not_scan_or_read_io(installed, monkeypatch):
    _, _, payload = installed
    def forbidden(*_args, **_kwargs):
        pytest.fail("fact tree performed request I/O")
    with monkeypatch.context() as guard:
        for obj, name in [(profiles, "profile_project_trees"), (profiles, "_scoped_profile_targets"),
                          (profiles, "_read_profile_db"), (sqlite3, "connect"), (builtins, "open"),
                          (Path, "open"), (Path, "stat"), (socket.socket, "connect"), (subprocess, "Popen")]:
            guard.setattr(obj, name, forbidden)
        assert profiles.get_profiles_projects_tree() == payload
        response = asyncio.run(profiles.get_profiles_projects_tree.http_endpoint())
        assert json.loads(response.body) == payload


def test_tree_filters_and_surface_remain_distinct(installed):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as failure:
        profiles.get_profiles_projects_tree(preview_limit=4)
    assert failure.value.status_code == 503
    with pytest.raises(HTTPException):
        profiles.get_profiles_projects_tree(profile="foreign")


def test_missing_source_is_not_empty_success(installed):
    from fastapi import HTTPException
    store, key, _ = installed
    store.publish([Fact(key, {"encoded": b'{}'}, health="unknown", error_code="source_disappeared")])
    with pytest.raises(HTTPException) as failure:
        profiles.get_profiles_projects_tree()
    assert failure.value.detail["reason"] == "source_disappeared"
    store.publish(deleted_keys=[key])
    with pytest.raises(HTTPException) as failure:
        profiles.get_profiles_projects_tree()
    assert failure.value.detail["reason"] == "not_materialized"


def test_legacy_binding_restores_original_tree_once(installed, monkeypatch):
    calls = []
    monkeypatch.setattr(profiles, "_SIDEBAR_CACHE_TTL_SECONDS", 0)
    monkeypatch.setattr(profiles, "profile_project_trees", lambda *a, **k: calls.append("legacy") or [])
    store, _, _ = installed
    projects_fact_routes.install(store, lambda _: None)
    assert profiles.get_profiles_projects_tree() == {
        "projects": [], "active_id": None, "scoped_session_ids": [], "errors": []}
    assert calls == ["legacy"]


@pytest.mark.parametrize("http", [False, True])
def test_tree_pins_binding_across_fast_path_and_detached_reader(monkeypatch, http):
    from contextlib import contextmanager

    store = FakeStore()
    calls, leases = [], []

    class Bound:
        def read(self, *_):
            return None

        @contextmanager
        def legacy(self):
            leases.append("entered")
            yield
            leases.append("left")

    first = Bound()

    def provider(profile):
        calls.append(profile)
        return first if len(calls) == 1 else store.Unavailable("registry_changed")

    monkeypatch.setattr(projects_fact_routes, "_installed", (store, provider))
    monkeypatch.setattr(profiles, "_SIDEBAR_CACHE_TTL_SECONDS", 0)
    monkeypatch.setattr(profiles, "profile_project_trees", lambda *a, **k: [])
    if http:
        result = asyncio.run(profiles.get_profiles_projects_tree.http_endpoint())
        assert json.loads(result.body)["projects"] == []
    else:
        assert profiles.get_profiles_projects_tree()["projects"] == []
    assert calls == [None]
    assert leases.count("entered") == leases.count("left") == 2
    # The next request resolves afresh; no pin leaks from the completed request.
    from fastapi import HTTPException
    with pytest.raises(HTTPException, match="registry_changed"):
        profiles.get_profiles_projects_tree()
    assert calls == [None, None]


def test_cancelled_http_waiter_does_not_release_actual_tree_reader(monkeypatch):
    import threading
    from plugins.kanban.dashboard.board_fact_views import bind

    store = FakeStore()
    scope = Scope("install", "profile")
    entered, finish, draining = threading.Event(), threading.Event(), threading.Event()
    switched = threading.Event()
    errors = []
    bound = bind(store, scope, stop_legacy=lambda: None, start_legacy=lambda: None,
                 demand=lambda *a: None, hot_selectors=[])
    monkeypatch.setattr(projects_fact_routes, "_installed", (store, lambda _: bound))
    monkeypatch.setattr(profiles, "_SIDEBAR_CACHE_TTL_SECONDS", 0)

    def tree(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        return []

    monkeypatch.setattr(profiles, "profile_project_trees", tree)
    wait_for = bound._readers.wait_for

    def observed_wait(predicate, timeout):
        draining.set()
        return wait_for(predicate, timeout)

    monkeypatch.setattr(bound._readers, "wait_for", observed_wait)

    def activate():
        try:
            bound.on_mode("facts", 1)
            switched.set()
        except (RuntimeError, TimeoutError) as exc:
            errors.append(exc)

    thread = threading.Thread(target=activate)

    async def exercise():
        request = asyncio.create_task(profiles.get_profiles_projects_tree.http_endpoint())
        assert await asyncio.to_thread(entered.wait, 5)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert bound._active >= 1
        thread.start()
        assert await asyncio.to_thread(draining.wait, 5)
        assert not switched.is_set()
        finish.set()
        await asyncio.to_thread(thread.join, 5)
        assert switched.is_set() and not errors
        assert bound._active == 0

    try:
        asyncio.run(exercise())
    finally:
        finish.set()
        if thread.ident is not None:
            thread.join(5)
        bound.close()
