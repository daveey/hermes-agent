"""Real scoped IPC transport parity; source materialization is coordinator-owned.

Only release authorization is a test driver. Reads, publication, reset paging,
subscriptions and demand use the real host/replica, never FakeStore.
"""
import asyncio
import builtins
import json
import socket
import sqlite3
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException

from hermes_cli import projects_fact_routes
from hermes_cli.fact_contract import Fact, Scope
from hermes_cli.fact_fake_lifecycle import FakeLifecycle
from hermes_cli.fact_host import FactHost, acquire
from hermes_cli.fact_replica import Endpoint
from hermes_cli.fact_store import FactStore, view_key
from hermes_cli.web_routers import profiles
from plugins.kanban.dashboard import board_fact_views as views
from tests.plugins import test_kanban_board_fact_routes as helpers
from tests.waiting import wait_until
from tui_gateway import server

api = helpers.api


@pytest.fixture
def real(tmp_path, monkeypatch):
    scope = Scope("install", "profile", "default")
    store = FactStore()
    token = "board-replica-test-capability-00000001"
    directory = tempfile.TemporaryDirectory(prefix="board-ipc-")
    host = FactHost(directory.name, {token: scope}, store=store).start()
    handle = acquire(scope, endpoint=Endpoint(str(host.directory / "facts.sock"), token))
    lifecycle = FakeLifecycle()
    transitions = []
    bound = views.bind(handle.v1, scope, lifecycle=lifecycle,
                       stop_legacy=lambda: transitions.append("stop"),
                       start_legacy=lambda: transitions.append("start"),
                       demand=handle.demand, hot_selectors=[("board", views.board_selector())])
    monkeypatch.setattr(views, "_installed", (handle.v1, lambda _: bound))
    monkeypatch.setattr(projects_fact_routes, "_installed", (handle.v1, lambda _: bound))
    for name in ("sessions", "panel", "annotations", "notifications"):
        lifecycle.bind_consumer(name, scope, lambda *_: None)
        lifecycle.report_ready(name, scope)

    def publish(name, selector, payload=None, *, health="fresh", delete=False):
        key = view_key(scope, name, selector)
        if delete:
            store.publish(deleted_keys=[key])
        else:
            store.publish([Fact(key, {"encoded": json.dumps(payload).encode()},
                                health=health, error_code="source_missing" if health != "fresh" else None)])
        revision = store.snapshot(scope).rev
        wait_until(lambda: handle.v1.snapshot(scope).rev == revision and handle._online,
                   what="real replica applied owner revision")
        return key

    try:
        yield scope, handle, bound, lifecycle, transitions, publish
    finally:
        bound.close()
        handle.close()
        host.close()
        directory.cleanup()


def activate(real):
    scope, _, bound, lifecycle, _, publish = real
    publish("board", views.board_selector(), {"columns": [], "now": 1000})
    assert bound.prepare()
    lifecycle.activate(scope)


def test_real_replica_all_exported_views_and_rollback(api, real, monkeypatch):
    activate(real)
    scope, handle, bound, lifecycle, transitions, publish = real
    tree = {"projects": [], "active_id": None, "scoped_session_ids": [], "errors": []}
    rest = {"surface": "profiles", "preview_limit": 3, "session_limit": 2000, "profile": None}
    native = {**rest, "surface": "native"}
    sessions = {"surface": "native", "profile": None, "project_id": "p1", "session_limit": 5000}
    publish("boards", {"include_archived": False}, {"boards": [], "current": "default"})
    publish("projects", {}, {"projects": []})
    publish("tree", rest, tree)
    publish("tree", native, tree)
    publish("project_sessions", sessions, {"project": None})
    request_thread = threading.get_ident()

    def wrap(original):
        def guarded(*args, **kwargs):
            assert threading.get_ident() != request_thread, "request performed I/O"
            return original(*args, **kwargs)
        return guarded

    def forbidden(*args, **kwargs):
        pytest.fail("warmed route entered legacy reader")

    with monkeypatch.context() as guard:
        for obj, name in [(sqlite3, "connect"), (builtins, "open"), (Path, "open"),
                          (Path, "stat"), (socket.socket, "connect"), (subprocess, "Popen")]:
            guard.setattr(obj, name, wrap(getattr(obj, name)))
        for obj, name in [(api, "_get_board_uncached"), (api, "_resolve_board"),
                          (profiles, "profile_project_trees"), (profiles, "_read_profile_db")]:
            guard.setattr(obj, name, forbidden)
        assert api.get_board() == {"columns": [], "now": 1000}
        assert json.loads(asyncio.run(api.get_board_endpoint()).body) == api.get_board()
        assert json.loads(api.list_boards().body) == {"boards": [], "current": "default"}
        assert json.loads(api.list_kanban_projects().body) == {"projects": []}
        assert profiles.get_profiles_projects_tree() == tree
        assert json.loads(asyncio.run(profiles.get_profiles_projects_tree.http_endpoint()).body) == tree
    # Existing native auth can resolve a profile; keep it intact, guard data readers.
    with monkeypatch.context() as guard:
        guard.setattr(server, "_profile_db", forbidden)
        guard.setattr(server, "_stamped_project_tree", forbidden)
        assert server._methods["projects.tree"](1, {})["result"] == tree
        assert server._methods["projects.project_sessions"](2, {"project_id": "p1"})["result"] == {"project": None}
    with pytest.raises(ValueError, match="unauthorized_scope"):
        handle.v1.snapshot(Scope("install", "foreign", "default"))
    lifecycle.rollback(scope)
    lifecycle.rollback(scope)
    assert transitions == ["start", "stop", "start"]
    assert bound.read("tree", rest) is None
    assert bound.read("project_sessions", sessions) is None
    assert bound.read("board", views.board_selector()) is None


def test_real_replica_frozen_canonical_board_graph_delete(api, real, monkeypatch):
    import time
    from hermes_cli import kanban_db, kanban_db_connect

    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)
    kanban_db.init_db()
    with kanban_db_connect.connect() as conn:
        parent = kanban_db.create_task(conn, title="parent", assignee="default", tenant="demo")
        child = kanban_db.create_task(conn, title="child", assignee="default", tenant="demo")
        kanban_db.link_tasks(conn, parent, child)
        conn.commit()
    selector = views.board_selector("demo", False, None, None, 80)
    expected = api.get_board(board="default", **selector)
    activate(real)
    publish = real[-1]
    publish("board", selector, expected)
    assert api.get_board(board="default", **selector) == expected
    with kanban_db_connect.connect() as conn:
        conn.execute("DELETE FROM task_links WHERE child_id=?", (child,))
        conn.execute("DELETE FROM tasks WHERE id=?", (child,))
        conn.commit()
    # Canonical source capture runs explicitly off-request, not in the replica.
    changed = api._get_board_uncached("demo", False, "default", None, None, 80)
    publish("board", selector, changed)
    assert api.get_board(board="default", **selector) == changed
    cards = [card for column in changed["columns"] for card in column["tasks"]]
    assert [card["id"] for card in cards] == [parent]
    assert cards[0]["link_counts"]["children"] == 0
    assert changed["now"] == expected["now"] == 1_700_000_000


def test_real_replica_delta_health_delete_and_filters(api, real):
    activate(real)
    _, _, _, _, _, publish = real
    selector = views.board_selector("tenant", True, "flow", "step", 7)
    payload = {"columns": [{"name": "ready", "tasks": [{"id": "parent", "children": ["child"]}]}], "now": 1000}
    publish("board", selector, payload)
    assert api.get_board(board="default", **selector) == payload
    with pytest.raises(HTTPException):
        api.get_board(board="default", **{**selector, "done_limit": 8})
    changed = {"columns": [], "now": 1000}
    publish("board", selector, changed)
    assert api.get_board(board="default", **selector) == changed
    publish("board", selector, changed, health="unknown")
    with pytest.raises(HTTPException) as error:
        api.get_board(board="default", **selector)
    assert error.value.detail["reason"] == "source_missing"
    publish("board", selector, delete=True)
    with pytest.raises(HTTPException) as error:
        api.get_board(board="default", **selector)
    assert error.value.detail["reason"] == "not_materialized"
    publish("board", selector, changed)
    assert api.get_board(board="default", **selector) == changed
