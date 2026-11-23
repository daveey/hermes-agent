"""Wire parity against legacy serializers and request-time I/O prohibition.

Producer source hydration/index correctness has its own suite. These tests seed
real session stores, capture unchanged legacy serializers, publish those bytes
through the shared FakeStore and exercise the mounted consumer. [daveey]
"""
import builtins
import inspect
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from hermes_cli.fact_contract import Fact, Scope
from hermes_cli.fact_fake import FakeStore
from hermes_cli.sessions_fact_routes import selector_for, use
from tests.hermes_cli import test_profiles_sidebar_scope as scope_helpers

profiles_on_disk = scope_helpers.profiles_on_disk


@pytest.fixture
def seeded(profiles_on_disk, monkeypatch):
    from hermes_state import SessionDB
    from hermes_cli.web_routers import profiles

    for name, home in profiles_on_disk.items():
        db = SessionDB(db_path=home / "state.db")
        try:
            for index in range(610 if name == "default" else 6):
                sid = f"session-{index}"
                db.create_session(sid, source=("cli", "cron", "telegram")[index % 3],
                                  system_prompt="full-only fixture")
                db.append_message(session_id=sid, role="user", content=f"hello {index}")
                if index % 2:
                    db.append_message(session_id=sid, role="assistant", content="reply")
                if index in (0, 3):
                    db.set_session_pinned(sid, True)
                if index == 3:
                    db.set_session_archived(sid, True)
        finally:
            db.close()
    monkeypatch.setattr(profiles, "_SIDEBAR_CACHE_TTL_SECONDS", 0)
    # Distinct annotations prove none are lost/reordered by the encoded adapter.
    for name in ("kanban", "goal", "cron", "background", "turn", "subagent", "review"):
        def annotate(rows, _name=name, **kwargs):
            for row in rows:
                row.setdefault("fixture_annotation_order", []).append(_name)
                row[f"fixture_{_name}"] = {"nullable": None, "values": [True, 0, ""]}
            return rows
        attr = "annotate_review_wait" if name == "review" else f"annotate_{name}_activity"
        monkeypatch.setattr(profiles, attr, annotate)
    return profiles


def mount(profiles):
    app = FastAPI()
    app.include_router(profiles.sessions_router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def install(profiles):
    store = FakeStore()
    scope = Scope("fixture-install", "authorized-aggregate")
    demands = []
    handle = SimpleNamespace(v1=store, demand=lambda name, selector: demands.append((name, selector)))
    binding = profiles.bind_sessions_facts(handle, scope)
    for name in ("panel", "annotations", "board", "notifications"):
        store.bind_consumer(name, scope, lambda mode, generation: None)
    for name in ("sessions", "panel", "annotations", "board", "notifications"):
        store.report_ready(name, scope, version=1, ready=True)
    return store, scope, demands, binding


def forbid_request_io(monkeypatch, profiles):
    def forbidden(*args, **kwargs):
        pytest.fail("fact request attempted legacy helper or I/O")

    for owner, names in (
        (builtins, ("open",)), (io, ("open",)),
        (os, ("open", "stat", "scandir", "listdir")),
        (Path, ("open", "read_text", "read_bytes", "stat", "iterdir")),
        (sqlite3, ("connect",)), (subprocess, ("Popen", "run", "check_output")),
        (socket, ("create_connection", "getaddrinfo")), (socket.socket, ("connect", "connect_ex")),
        (profiles, ("_read_profile_db", "_read_serial_profile_db", "_profile_targets",
                    "_cron_profile_home", "_corrupt_profile_stores", "_sidebar_profile_cache_get",
                    "annotate_kanban_activity", "annotate_goal_activity", "annotate_cron_activity",
                    "annotate_background_activity", "annotate_turn_activity", "annotate_subagent_activity",
                    "annotate_review_wait", "automated_session_sources", "native_json_response")),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)


@pytest.mark.asyncio
async def test_all_session_routes_exact_wire_parity_without_request_io(seeded, monkeypatch):
    profiles = seeded
    cases = [
        ("sessions", "/api/profiles/sessions", {"limit": 20}),
        ("sessions", "/api/profiles/sessions", {"limit": 20, "offset": 580}),
        ("sessions", "/api/profiles/sessions", {"limit": 8, "min_messages": 2, "source": "cli"}),
        ("sessions", "/api/profiles/sessions", {"archived": "only", "full": True}),
        ("sessions", "/api/profiles/sessions", {"sources": "cli,telegram", "exclude_sources": "cron",
                                                   "profile": "worker", "order": "created", "automated": "exclude"}),
        ("sessions", "/api/profiles/sessions", {"archived": "include", "limit": 0}),
        ("sessions_sidebar", "/api/profiles/sessions/sidebar", {"recents_profile": "all"}),
        ("sessions_sidebar", "/api/profiles/sessions/sidebar", {"recents_profile": "worker", "cron_limit": 1}),
        ("sessions_stamps", "/api/profiles/sessions/stamps", {"ids": "session-0,missing,session-3", "profile": "worker"}),
    ]
    routes = {route.path: route.endpoint for route in profiles.sessions_router.routes}
    async with mount(profiles) as client:
        expected = []
        for name, path, params in cases:
            response = await client.get(path, params=params)
            assert response.status_code == 200
            selector = selector_for(inspect.signature(routes[path]), (), params)
            expected.append((name, path, params, selector, response.content))
        assert len(json.loads(expected[1][-1])["sessions"]) >= 20
        store, scope, demands, binding = install(profiles)
        try:
            store.publish([Fact(store.view_key(scope, name, selector), {"encoded": body}, health="fresh")
                           for name, _path, _params, selector, body in expected])
            store.activate(scope)
            with use(binding), monkeypatch.context() as guard:
                forbid_request_io(guard, profiles)
                for _name, path, params, _selector, body in expected:
                    response = await client.get(path, params=params)
                    assert response.status_code == 200
                    assert response.content == body
                    assert response.headers["content-type"] == "application/json"
                    assert int(response.headers["content-length"]) == len(body)
            assert demands == []
        finally:
            store.rollback(scope)
            binding.close()


@pytest.mark.asyncio
async def test_cold_stale_and_wrong_scope_never_fall_back(monkeypatch):
    from hermes_cli.web_routers import profiles

    async with mount(profiles) as client:
        store, scope, demands, binding = install(profiles)
        selector = selector_for(inspect.signature(profiles.get_profiles_sessions), (), {})
        other = Scope("other-install", scope.profile_id)
        store.publish([Fact(store.view_key(other, "sessions", selector), {"encoded": b'{"private":true}'}, health="fresh")])
        try:
            store.activate(scope)
            # Warm FastAPI's endpoint-source diagnostic cache before the guard.
            with use(binding):
                assert (await client.get("/api/profiles/sessions")).status_code == 503
            demands.clear()
            with monkeypatch.context() as guard:
                forbid_request_io(guard, profiles)
                missing = await client.get("/api/profiles/sessions")
            assert missing.status_code == 503
            assert demands == []
            with use(binding), monkeypatch.context() as guard:
                forbid_request_io(guard, profiles)
                cold = await client.get("/api/profiles/sessions")
            assert cold.status_code == 503
            assert cold.headers["retry-after"] == "1"
            assert demands == [("sessions", selector)]
            store.publish([Fact(store.view_key(scope, "sessions", selector), {"encoded": b"{}"}, health="stale")])
            with use(binding), monkeypatch.context() as guard:
                forbid_request_io(guard, profiles)
                stale = await client.get("/api/profiles/sessions")
            assert stale.status_code == 503
            assert len(demands) == 1
            # Background owner completes the requested materialization, not the route.
            store.publish([Fact(store.view_key(scope, "sessions", selector), {"encoded": b"{}"}, health="fresh")])
            with use(binding), monkeypatch.context() as guard:
                forbid_request_io(guard, profiles)
                ready = await client.get("/api/profiles/sessions")
            assert ready.status_code == 200 and ready.content == b"{}"
        finally:
            store.rollback(scope)
            binding.close()
