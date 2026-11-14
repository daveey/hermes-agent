"""JSON list routes encode in the worker thread, never with jsonable_encoder on the loop. [daveey]

``/api/profiles/sessions`` (Desktop polls it ~0.7/s with limit=200) spent most of its
CPU in FastAPI's ``serialize_response -> jsonable_encoder`` walk on the event loop.
The routes now build the ``JSONResponse`` in the pool thread that produced the
payload (``hermes_cli.web_json_response``), with byte-identical output.
"""
import asyncio
import copy
import dataclasses
import datetime
import enum
import inspect
import math
import threading
from pathlib import Path

import pytest
from fastapi.encoders import jsonable_encoder
from starlette.responses import JSONResponse

from hermes_cli.web_json_response import (
    encoded_in_thread, native_json_response, per_request_response,
)


def _rows(n=220):
    """Deterministic, realistic session rows: unicode, None, floats, nesting, int keys."""
    rows = []
    for i in range(n):
        rows.append({
            "id": f"20261112_{i:06d}_{i * 7919 % 65536:04x}",
            "title": ["Fix the café ☕ build", "日本語のタイトル", "emoji 🚀🔥 run", None, "quote \" and \\ slash"][i % 5],
            "source": ["desktop", "cron", "telegram", "kanban"][i % 4],
            "model": "claude-opus-5-5" if i % 3 else None,
            "started_at": 1794400000.0 + i * 13.25,
            "last_active": 1794460000.5 - i * 0.1 if i % 7 else None,
            "ended_at": None if i % 2 else 1794460100.75,
            "message_count": i * 3,
            "tool_call_count": i % 11,
            "input_tokens": 10 ** 9 + i,
            "estimated_cost_usd": round(i * 0.0137, 6),
            "ratio": i / 3,
            "archived": bool(i % 9 == 0),
            "pinned": i % 50 == 0,
            "is_active": i % 4 == 0,
            "profile": "default" if i % 6 else "worker",
            "tags": ["a", "b\u00e9", i, None, 1.5][: i % 6],
            "lineage": (f"p{i}", f"q{i}"),
            "kanban": {"task_id": f"t_{i:x}", "status": "running", "counts": {1: i, 2: None, 3: [True, False]}},
            "preview": {"role": "user", "content": "x" * (i % 40), "nested": [[{"k": i}], {}]},
        })
    return rows


def _payload(rows=None):
    rows = _rows() if rows is None else rows
    return {"sessions": rows, "total": len(rows), "profile_totals": {"default": len(rows) - 37, "worker": 37},
            "limit": 200, "offset": 0, "errors": [], "storage": {}}


def _parent(payload):
    return JSONResponse(jsonable_encoder(payload))


def _same_response(a, b):
    assert a.status_code == b.status_code
    assert a.raw_headers == b.raw_headers
    assert a.body == b.body


def test_native_payload_matches_parent_bytes():
    payload = _payload()
    assert len(payload["sessions"]) >= 200
    expected = _parent(copy.deepcopy(payload))
    got = native_json_response(payload)
    assert type(got) is JSONResponse
    _same_response(got, expected)
    # The per-request copy sent on the loop carries the same status, headers and body.
    fresh = per_request_response(got)
    assert fresh is not got
    _same_response(fresh, expected)


def test_response_passes_through_and_fallback_is_unchanged(monkeypatch):
    response = JSONResponse({"a": 1})
    rendered = []
    real_render = JSONResponse.render
    monkeypatch.setattr(JSONResponse, "render", lambda self, content: rendered.append(content) or real_render(self, content))
    assert native_json_response(response) is response
    assert rendered == [], "an existing Response was re-encoded"
    monkeypatch.undo()
    assert per_request_response(response) is not response  # our own shape: cloned per request
    payload = {"when": datetime.datetime(2026, 11, 12)}
    assert native_json_response(payload) is payload
    assert per_request_response(payload) is payload


class _Color(enum.Enum):
    RED = "red"


@dataclasses.dataclass
class _Point:
    x: int
    y: float


_NON_NATIVE = {
    "datetime": {"sessions": [{"at": datetime.datetime(2026, 11, 12, 7, 46, 11)}]},
    "set": {"ids": {3}},
    "path": {"cwd": Path("/home/you/code")},
    "dataclass": {"pt": _Point(1, 2.5)},
    "enum": {"color": _Color.RED},
    "nan": {"cost": math.nan},
    "inf": {"cost": [1.0, -math.inf]},
    "bytes": {"blob": b"abc"},
}


@pytest.mark.parametrize("name", sorted(_NON_NATIVE))
def test_non_native_payload_takes_parent_path(name):
    """A payload json.dumps refuses goes back to FastAPI unchanged: same response or same error."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    payload = _NON_NATIVE[name]
    assert native_json_response(payload) is payload

    app = FastAPI()
    app.add_api_route("/parent", lambda: payload, methods=["GET"])

    encoded = encoded_in_thread(lambda: payload)

    async def candidate():
        return per_request_response(await asyncio.to_thread(encoded))
    app.add_api_route("/candidate", candidate, methods=["GET"])

    outcomes = []
    with TestClient(app) as client:
        for path in ("/parent", "/candidate"):
            try:
                r = client.get(path)
            except Exception as exc:  # health: allow BLE001 -- the parent's own error is the expected outcome
                outcomes.append(("raised", type(exc), str(exc)))
            else:
                outcomes.append(("response", r.status_code, sorted(r.headers.items()), r.content))
    assert outcomes[0] == outcomes[1]
    if name in ("nan", "inf"):
        assert outcomes[0][0] == "raised" and outcomes[0][1] is ValueError
    else:
        assert outcomes[0][0] == "response" and outcomes[0][1] == 200


def test_encoded_in_thread_keeps_signature_and_hints():
    from hermes_cli.web_routers import profiles, sessions
    for func in (profiles.get_profiles_sessions, sessions.get_sessions):
        assert inspect.signature(encoded_in_thread(func)) == inspect.signature(func)
    routes = {r.path: r for r in profiles.sessions_router.routes}
    assert (inspect.signature(routes["/api/profiles/sessions"].endpoint)
            == inspect.signature(profiles.get_profiles_sessions))
    list_routes = {r.path: r for r in sessions.list_router.routes}
    assert inspect.signature(list_routes["/api/sessions"].endpoint) == inspect.signature(sessions.get_sessions)


# ── Real routers ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def routed(monkeypatch, tmp_path):
    """The real profiles/sessions routers over deterministic fixture rows."""
    from hermes_cli.web_routers import profiles, sessions

    rows = _rows()
    state = {"release": threading.Event(), "entered": threading.Event(), "reads": 0, "block": False}
    state["release"].set()

    def scan(name, home, errors, read, cache_key):
        state["reads"] += 1
        state["entered"].set()
        if state["block"]:
            assert state["release"].wait(5)
        return copy.deepcopy(rows), len(rows)

    def sidebar_read(name, home, errors, callback):
        return {"recents": copy.deepcopy(rows[:50]), "cron": copy.deepcopy(rows[50:60]),
                "messaging": copy.deepcopy(rows[60:80]), "usage": {"tokens": 12.5}}

    def project_trees(*args, **kwargs):
        return [("default", {"projects": [{"id": "p1", "path": "/home/you/code/x", "repos": [],
                                            "previewSessions": copy.deepcopy(rows[:3]), "lastActive": 2.5}],
                             "scoped_session_ids": ["a", "b"]})]

    def list_payload(*args):
        return {"sessions": copy.deepcopy(rows[:100]), "total": 100, "limit": args[0], "offset": 0, "storage": {}}

    db_path = tmp_path / "state.db"
    db_path.touch()
    monkeypatch.setattr(profiles, "_profile_targets", lambda label: [("default", None)])
    monkeypatch.setattr(profiles, "_read_serial_profile_db", scan)
    monkeypatch.setattr(profiles, "_read_profile_db", sidebar_read)
    monkeypatch.setattr(profiles, "_tag_rows", lambda rows, name, now: rows)
    monkeypatch.setattr(profiles, "stamp_automated", lambda rows, *a, **kw: rows)
    monkeypatch.setattr(profiles, "_corrupt_profile_stores", lambda targets: {})
    monkeypatch.setattr(profiles, "_profile_state_db", lambda home: db_path)
    monkeypatch.setattr(profiles, "subagent_listing_scope", lambda *a, **kw: (False, None))
    monkeypatch.setattr(profiles, "_sidebar_profile_cache_get", lambda key: None)
    monkeypatch.setattr(profiles, "automated_session_sources", lambda home: set())
    monkeypatch.setattr(profiles, "profile_project_trees", project_trees)
    for name in ("kanban", "goal", "cron", "background", "turn"):
        monkeypatch.setattr(profiles, f"annotate_{name}_activity", lambda rows, **kw: rows)
    monkeypatch.setattr(profiles, "annotate_review_wait", lambda rows: rows)
    monkeypatch.setattr(sessions, "_session_db_path_for_profile", lambda profile: db_path)
    monkeypatch.setattr(sessions, "_list_sessions_payload", list_payload)
    monkeypatch.setattr(sessions, "data_stamp", lambda path: ("stable",))
    sessions.SESSION_LIST_CACHE.clear()
    yield state
    sessions.SESSION_LIST_CACHE.clear()


def _app(cors=False):
    from fastapi import FastAPI
    from hermes_cli.web_routers import profiles, sessions
    app = FastAPI()
    app.include_router(profiles.sessions_router)
    app.include_router(sessions.list_router)
    if cors:
        from starlette.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, allow_origins=["http://desktop.test"], allow_methods=["*"])
    return app


@pytest.fixture
def encode_spy(monkeypatch):
    """Count jsonable_encoder calls and record the threads JSONResponse.render ran on."""
    import fastapi.encoders
    import fastapi.routing

    spy = {"jsonable": 0, "render_threads": []}
    real_jsonable = fastapi.encoders.jsonable_encoder
    real_render = JSONResponse.render

    def jsonable(*args, **kwargs):
        spy["jsonable"] += 1
        return real_jsonable(*args, **kwargs)

    def render(self, content):
        spy["render_threads"].append(threading.get_ident())
        return real_render(self, content)

    monkeypatch.setattr(fastapi.encoders, "jsonable_encoder", jsonable)
    monkeypatch.setattr(fastapi.routing, "jsonable_encoder", jsonable)
    monkeypatch.setattr(JSONResponse, "render", render)
    return spy


@pytest.mark.asyncio
@pytest.mark.parametrize("route,params", [
    ("/api/profiles/sessions", {"limit": 200}),
    ("/api/profiles/sessions/sidebar", {"recents_limit": 50}),
    ("/api/profiles/projects/tree", {"preview_limit": 3}),
    ("/api/sessions", {"limit": 100}),
])
async def test_route_encodes_off_loop_without_jsonable_encoder(routed, encode_spy, route, params):
    import httpx
    loop_thread = threading.get_ident()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        first = await client.get(route, params=params)
        # The second request is a cache hit for the cached routes (sidebar, tree, /api/sessions).
        second = await client.get(route, params=params)
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert encode_spy["jsonable"] == 0, "FastAPI's jsonable_encoder ran for a JSON-native payload"
    assert encode_spy["render_threads"], "the route did not build a JSONResponse"
    assert loop_thread not in encode_spy["render_threads"], "the JSON encode ran on the event-loop thread"
    assert first.headers["content-type"] == "application/json"
    assert int(first.headers["content-length"]) == len(first.content)


@pytest.mark.asyncio
async def test_profiles_sessions_body_matches_parent_path(routed):
    import httpx
    from hermes_cli.web_routers import profiles
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        got = await client.get("/api/profiles/sessions", params={"limit": 200})
        bad_archived = await client.get("/api/profiles/sessions", params={"archived": "bogus"})
        bad_order = await client.get("/api/profiles/sessions", params={"order": "bogus"})
        too_many = await client.get("/api/profiles/sessions", params={"limit": 501})
        negative = await client.get("/api/profiles/sessions", params={"offset": -1})
        at_cap = await client.get("/api/profiles/sessions", params={"limit": 500})
    expected = _parent(profiles.get_profiles_sessions(
        limit=200, offset=0, min_messages=0, archived="exclude", order="recent", profile="all",
        source=None, sources=None, exclude_sources=None, full=False))
    assert len(got.json()["sessions"]) >= 200
    assert got.status_code == expected.status_code
    assert got.content == expected.body
    assert [(k.lower(), v) for k, v in got.headers.raw] == expected.raw_headers
    assert bad_archived.status_code == 400 and "archived must be one of" in bad_archived.json()["detail"]
    assert bad_order.status_code == 400 and "order must be one of" in bad_order.json()["detail"]
    assert too_many.status_code == 422 and negative.status_code == 422
    assert at_cap.status_code == 200


@pytest.mark.asyncio
async def test_coalesced_requests_each_get_full_body_and_own_headers(routed, monkeypatch):
    """Two requests joined on one read share the rendered body, not one Response object.

    FastAPI sets ``response.background`` on a returned Response, so one shared
    object would carry the first request's background tasks into the joined one.
    """
    import httpx
    from starlette.responses import Response
    from tests.waiting import async_wait_until

    sent = []
    real_call = Response.__call__

    async def recording_call(self, scope, receive, send):
        sent.append(self)
        return await real_call(self, scope, receive, send)

    monkeypatch.setattr(Response, "__call__", recording_call)
    routed["block"] = True
    routed["release"].clear()
    headers = {"Origin": "http://desktop.test"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(cors=True)),
                                 base_url="http://test") as client:
        first = asyncio.create_task(client.get("/api/profiles/sessions", params={"limit": 200}, headers=headers))
        await async_wait_until(routed["entered"].is_set, timeout=3)
        second = asyncio.create_task(client.get("/api/profiles/sessions", params={"limit": 200}, headers=headers))
        for _ in range(50):
            await asyncio.sleep(0)
        routed["release"].set()
        responses = await asyncio.gather(first, second)
        # A later, separate read after the joined pair still answers normally.
        third = await client.get("/api/profiles/sessions", params={"limit": 200}, headers=headers)
    assert routed["reads"] == 2, "the two concurrent requests did not join one read"
    assert len(sent) == 3 and len({id(r) for r in sent}) == 3, "joined requests sent one shared Response object"
    bodies = {r.content for r in [*responses, third]}
    assert len(bodies) == 1
    assert len(responses[0].json()["sessions"]) >= 200
    for r in [*responses, third]:
        assert r.status_code == 200
        assert r.headers.get_list("access-control-allow-origin") == ["http://desktop.test"]
        assert r.headers.get_list("vary") == ["Origin"]
        assert r.headers.get_list("content-length") == [str(len(r.content))]
