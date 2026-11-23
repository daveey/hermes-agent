"""Exercise exported adapters, including the mounted FastAPI signature contract.

These prove consumer transport parity. Producer source/reducer parity is a separate
integration gate: publication here deliberately uses the legacy serializer output.
"""
import asyncio
import builtins
import importlib.util
import json
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from hermes_cli import kanban_db, projects_db
from hermes_cli.fact_contract import Fact, Scope
from hermes_cli.fact_fake import FakeStore
from plugins.kanban.dashboard import board_fact_views as views


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_board_facts_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(views, "_installed", None)
    return module


@pytest.mark.parametrize("http", [False, True])
def test_board_pins_provider_and_current_selection(api, monkeypatch, http):
    store = FakeStore()
    calls, reads = [], []
    bound = views.bind(store, Scope("install", "profile", "original"),
                       stop_legacy=lambda: None, start_legacy=lambda: None,
                       demand=lambda *_: None, hot_selectors=[])

    def provider(board):
        calls.append(board)
        return bound if len(calls) == 1 else store.Unavailable("registry_changed")

    def legacy_read(*args):
        reads.append(args[2])
        assert bound._active >= 1
        return {"selected": args[2]}

    monkeypatch.setattr(views, "_installed", (store, provider))
    monkeypatch.setattr(api, "_get_board_uncached", legacy_read)
    monkeypatch.setattr(kanban_db, "get_current_board", lambda: "changed")
    try:
        if http:
            result = asyncio.run(api.get_board_endpoint(board=None, **views.board_selector()))
            assert json.loads(result.body) == {"selected": "original"}
        else:
            assert api.get_board(board=None, **views.board_selector()) == {"selected": "original"}
        assert calls == [None]
        assert reads == ["original"]
        assert bound._active == 0
        with pytest.raises(HTTPException, match="registry_changed"):
            api.get_board(board=None, **views.board_selector())
        assert calls == [None, None]
    finally:
        bound.close()


def test_unbound_current_board_request_does_not_rebind_in_worker(api, monkeypatch):
    store = FakeStore()
    calls = []

    def provider(board):
        calls.append(board)
        return None if len(calls) == 1 else store.Unavailable("registry_changed")

    monkeypatch.setattr(views, "_installed", (store, provider))
    monkeypatch.setattr(kanban_db, "get_current_board", lambda: "default")
    monkeypatch.setattr(api, "_get_board_uncached", lambda *args: {"selected": args[2]})
    result = asyncio.run(api.get_board_endpoint(board=None, **views.board_selector()))
    assert json.loads(result.body) == {"selected": "default"}
    assert calls == [None]


def test_frozen_legacy_board_payload_survives_fact_transport(api, monkeypatch):
    import time
    from hermes_cli import kanban_db_connect
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)
    kanban_db.init_db()
    with kanban_db_connect.connect() as conn:
        parent = kanban_db.create_task(conn, title="parent", assignee="default", tenant="demo")
        child = kanban_db.create_task(conn, title="child", assignee="default", tenant="demo")
        kanban_db.link_tasks(conn, parent, child)
        kanban_db.add_comment(conn, parent, "fixture", "comment")
        conn.commit()
    selector = views.board_selector("demo", False, None, None, 80)
    expected = api.get_board(board="default", **selector)
    store, scope, bound = install([("board", selector, expected)])
    try:
        assert api.get_board(board="default", **selector) == expected
        with kanban_db_connect.connect() as conn:
            conn.execute("DELETE FROM task_links WHERE child_id=?", (child,))
            conn.execute("DELETE FROM tasks WHERE id=?", (child,))
            conn.commit()
        # Reference serializer runs only as the isolated producer-side fixture.
        changed = api._get_board_uncached("demo", False, "default", None, None, 80)
        store.publish([Fact(store.view_key(scope, "board", selector),
                            {"encoded": json.dumps(changed).encode()}, health="fresh")])
        assert api.get_board(board="default", **selector) == changed
        cards = [card for column in changed["columns"] for card in column["tasks"]]
        assert [card["id"] for card in cards] == [parent]
        assert cards[0]["link_counts"]["children"] == 0
        assert changed["now"] == expected["now"] == 1_700_000_000
    finally:
        bound.close()


def test_captured_board_projection_is_canonical_and_source_free(api, monkeypatch):
    import time
    from hermes_cli import kanban_db_connect
    from plugins.kanban.dashboard import board_read

    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)
    kanban_db.init_db()
    with kanban_db_connect.connect() as conn:
        parent = kanban_db.create_task(conn, title="parent", assignee="default", tenant="demo")
        child = kanban_db.create_task(conn, title="child", assignee="default", tenant="demo")
        kanban_db.link_tasks(conn, parent, child)
        conn.commit()
        rows = board_read.materialize(conn, "demo", True, None, None, 80)
    expected = board_read.project(rows, True)
    captured: dict = dict(diagnostics_per_task=board_read.diagnostics_from_inputs(rows["diag_inputs"]),
                    ages={t.id: kanban_db.task_age(t) for t, _, _ in rows["cards"]},
                    assignees=expected["assignees"], now=1_700_000_000)

    def forbidden(*args, **kwargs):
        pytest.fail("pure board projection resolved a source or clock")

    with monkeypatch.context() as guard:
        for obj, name in [(time, "time"), (sqlite3, "connect"), (builtins, "open"),
                          (Path, "stat"), (Path, "open"), (subprocess, "Popen"),
                          (socket.socket, "connect"), (kanban_db, "task_age"),
                          (kanban_db, "list_profiles_on_disk"), (board_read, "diagnostics_from_inputs")]:
            guard.setattr(obj, name, forbidden)
        actual = board_read.project_captured(rows, True, **captured)
        assert actual == expected
        actual["assignees"].append("caller-mutation")
        assert "caller-mutation" not in captured["assignees"]
        actual["columns"][0]["tasks"].clear()
        assert board_read.project_captured(rows, True, **captured) == expected


def install(payloads):
    store = FakeStore()
    scope = Scope("install", "profile", "default")
    for name, selector, payload in payloads:
        store.publish([Fact(store.view_key(scope, name, selector),
                            {"encoded": json.dumps(payload).encode()}, health="fresh", observed_at=1000)])
    bound = views.bind(store, scope, stop_legacy=lambda: None, start_legacy=lambda: None,
                       demand=lambda *_: None, hot_selectors=[(n, s) for n, s, _ in payloads])
    for name in ("sessions", "panel", "annotations", "notifications"):
        store.bind_consumer(name, scope, lambda *_: None)
        store.report_ready(name, scope)
    assert bound.prepare()
    store.activate(scope)
    views.install_provider(store, lambda board: bound if board in (None, "default") else store.Unavailable("scope"))
    return store, scope, bound


def test_exported_warmed_reads_do_not_touch_io_or_encode(api, monkeypatch):
    payloads = [("board", views.board_selector(), {"columns": [], "now": 1000}),
                ("boards", {"include_archived": False}, {"boards": [], "current": "default"}),
                ("projects", {}, {"projects": []})]
    _, _, bound = install(payloads)
    dumps = json.dumps
    def selector_encoding_only(value, *args, **kwargs):
        # Canonicalizing a bounded selector is allowed; re-encoding payloads is not.
        assert isinstance(value, list) and len(value) == 2
        assert value[0] in {"board", "boards", "projects"} and isinstance(value[1], dict)
        return dumps(value, *args, **kwargs)
    def forbidden(*_args, **_kwargs):
        pytest.fail("warmed fact route performed legacy I/O or JSON encoding")
    try:
        with monkeypatch.context() as guard:
            guard.setattr(json, "dumps", selector_encoding_only)
            for obj, name in [(sqlite3, "connect"), (builtins, "open"), (Path, "open"), (Path, "stat"),
                              (socket.socket, "connect"), (subprocess, "Popen"),
                              (kanban_db, "get_current_board"), (kanban_db, "kanban_db_path"),
                              (projects_db, "connect_closing"), (api, "_get_board_uncached"),
                              (api, "_resolve_board"), (api, "_board_counts")]:
                guard.setattr(obj, name, forbidden)
            assert api.get_board() == payloads[0][2]
            assert json.loads(asyncio.run(api.get_board_endpoint()).body) == payloads[0][2]
            assert json.loads(api.list_boards().body) == payloads[1][2]
            assert json.loads(api.list_kanban_projects().body) == payloads[2][2]
    finally:
        bound.close()


def test_mounted_board_retains_all_query_args_and_authorized_scope(api):
    selector = views.board_selector("tenant", True, "flow", "step", 7)
    payload = {"columns": [{"name": "ready", "tasks": [{"id": "task"}]}], "now": 1000}
    _, _, bound = install([("board", selector, payload)])
    app = FastAPI()
    app.include_router(api.router)
    try:
        with TestClient(app) as client:
            result = client.get("/board", params={**selector, "board": "default"})
            assert result.status_code == 200, result.text
            assert result.json() == payload
            assert client.get("/board", params={**selector, "board": "foreign"}).status_code == 503
            assert client.get("/board", params={**selector, "done_limit": 8}).status_code == 503
    finally:
        bound.close()


def test_cancelled_board_http_keeps_worker_lease_until_read_finishes(api, monkeypatch):
    import threading

    store = FakeStore()
    scope = Scope("install", "profile", "default")
    entered, finish, draining = threading.Event(), threading.Event(), threading.Event()
    switched = threading.Event()
    errors = []
    bound = views.bind(store, scope, stop_legacy=lambda: None, start_legacy=lambda: None,
                       demand=lambda *_: None, hot_selectors=[])
    views.install_provider(store, lambda _: bound)

    def reader(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        return {"columns": []}

    monkeypatch.setattr(api, "_get_board_uncached", reader)
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
        request = asyncio.create_task(api.get_board_endpoint(
            tenant=None, include_archived=False, board="default", workflow_template_id=None,
            current_step_key=None, done_limit=80))
        assert await asyncio.to_thread(entered.wait, 5)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert bound._active >= 1  # detached coalesced worker, not the HTTP waiter
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


def test_pending_and_unknown_never_run_legacy(api, monkeypatch):
    store, scope, bound = install([("board", views.board_selector(), {"columns": []})])
    def forbidden(*_args, **_kwargs):
        pytest.fail("missing/unknown fact fell back to legacy")
    monkeypatch.setattr(api, "_get_board_uncached", forbidden)
    try:
        key = store.view_key(scope, "board", views.board_selector())
        store.publish([Fact(key, {"encoded": b'{}'}, health="unknown")])
        with pytest.raises(HTTPException) as failure:
            api.get_board()
        assert failure.value.status_code == 503
        store.publish(deleted_keys=[key])
        with pytest.raises(HTTPException) as failure:
            asyncio.run(api.get_board_endpoint())
        assert failure.value.status_code == 503
    finally:
        bound.close()
