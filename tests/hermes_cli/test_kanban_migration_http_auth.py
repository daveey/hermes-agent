"""Exercise migration through the existing dashboard auth middleware, not worker env."""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as db, web_server
from hermes_cli.dashboard_auth import middleware, token_auth
from hermes_cli.dashboard_auth.base import Session, TokenPrincipal
from hermes_cli.kanban_db_connect import connect_closing
from plugins.kanban.dashboard import plugin_api as api

REQUEST = dict(action="Approve staging", why_human="Owner decision", interaction="approval",
               scope="Staging only", evidence="Release policy")
PREFIX = "/api/plugins/kanban"


def snapshot(tid):
    with connect_closing() as conn:
        return (dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()),
                [tuple(r) for r in conn.execute("SELECT * FROM task_runs")],
                [tuple(r) for r in conn.execute("SELECT * FROM task_events")])


@pytest.mark.parametrize("surface", ["patch", "bulk"])
@pytest.mark.parametrize("mode", ["loopback", "session"])
@pytest.mark.parametrize("credential", ["valid", "missing", "invalid", "service", "spoofed", "query"])
def test_migration_http_boundary(monkeypatch, surface, mode, credential):
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "test-loopback")
    session = Session(user_id="owner-42", email="", display_name="Owner", org_id="",
                      provider="test-idp", expires_at=9999999999,
                      access_token="test-session", refresh_token="")
    # Stub only the external identity provider; keep token scanning, middleware,
    # request state and the sensitive-endpoint authorization check real.
    provider = SimpleNamespace(name="test-idp", verify_session=lambda *, access_token:
                               session if access_token == "test-session" else None)
    monkeypatch.setattr(middleware, "list_session_providers", lambda: [provider])
    from hermes_cli.dashboard_auth import request_utils
    monkeypatch.setattr(request_utils, "list_session_providers", lambda: [provider])
    app = FastAPI()
    app.state.auth_required = mode == "session"
    app.include_router(api.router, prefix=PREFIX)
    app.middleware("http")(web_server.auth_middleware)
    app.middleware("http")(middleware.gated_auth_middleware)
    app.middleware("http")(token_auth.token_auth_middleware)
    with connect_closing() as conn:
        tid = db.create_task(conn, title="Legacy", assignee="owner", initial_status="blocked")
    path = f"{PREFIX}/tasks/{tid}" if surface == "patch" else f"{PREFIX}/tasks/bulk"
    if credential == "service":
        monkeypatch.setattr(token_auth, "_token_routes", {path})
        service_provider = SimpleNamespace(name="service", verify_token=lambda *, token:
            TokenPrincipal(principal="worker-1", provider="service"))
        monkeypatch.setattr(token_auth, "list_token_providers", lambda: [service_provider])
    headers = {}
    if credential in {"valid", "invalid", "service"}:
        value = ("test-session" if mode == "session" else "test-loopback") if credential == "valid" else "worker-token"
        headers["Authorization"] = f"Bearer {value}"
    if credential == "spoofed":
        headers = {"X-Actor": "dashboard", "X-User-Id": "owner-42", "X-Hermes-Kanban-Task": tid}
    if credential == "query":
        path += "?token=test-loopback"
    payload = dict(metadata_only=True, human_request=REQUEST, block_reason="Reviewed")
    if surface == "bulk":
        payload["ids"] = [tid]
    before, runs, events = snapshot(tid)
    with TestClient(app) as client:
        response = getattr(client, "patch" if surface == "patch" else "post")(path, json=payload, headers=headers)
    after, after_runs, after_events = snapshot(tid)
    assert after_runs == runs
    if credential != "valid":
        assert response.status_code == 401, response.text
        assert after == before
        assert after_events == events
        return
    assert response.status_code == 200, response.text
    assert json.loads(after.pop("human_request")) == REQUEST
    before.pop("human_request")
    assert after == before  # no status/run/counter/authorization change
    assert len(after_events) == len(events) + 1
    with connect_closing() as conn:
        payload = db.list_events(conn, tid)[-1].payload
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["actor"] == ("dashboard:test-idp:owner-42" if mode == "session" else "dashboard:loopback-token")
    assert payload["metadata_only"] is True
    assert "test-session" not in json.dumps(payload)
    assert "test-loopback" not in json.dumps(payload)
