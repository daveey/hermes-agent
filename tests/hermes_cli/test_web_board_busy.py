"""Cold board reads must fail as retryable HTTP, never a false empty list. [daveey]"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_session_activity_busy as busy
from hermes_cli.kanban_generation import BoardLifecycleBusy


@pytest.fixture(params=["/api/session-kanban-tasks", "/api/plugins/user-steps/session-cards"])
def endpoint(request):
    return request.param


@pytest.fixture
def client(monkeypatch, tmp_path):
    from hermes_cli import web_server as ws

    busy.reset()
    from unittest.mock import AsyncMock

    # The independently shipped fixture plugin is enabled and runs in this profile.
    monkeypatch.setattr(ws, "_plugin_api_request_refused", AsyncMock(return_value=False))
    monkeypatch.setattr(ws, "forward_secondary_plugin_request", AsyncMock(return_value=None))
    # A dynamically included plugin router inherits the same exception boundary.
    # user-steps is independently shipped; this is its session-cards read contract.
    from fastapi import APIRouter

    plugin = APIRouter()

    @plugin.get("/api/plugins/user-steps/session-cards")
    def session_cards(ids: str):
        return {"tasks": ksa.session_tasks([ids], tuple(ksa.LISTED_STATUSES) + ("done",), stale_ok=True),
                "includes": ["done"]}

    # Insert before the dashboard's final catch-all, like plugin mounting does.
    monkeypatch.setattr(ws.app.router, "routes", list(plugin.routes) + list(ws.app.router.routes))
    monkeypatch.setattr(ksa, "_board_db_paths", lambda: [tmp_path / "board.db"])
    monkeypatch.setattr(ksa, "_remote_cards", lambda *a, **k: [])
    # Exercise the exhausted request budget without a real sleep.
    monkeypatch.setattr(busy, "request_deadline", lambda: 0.0)
    c = TestClient(ws.app, raise_server_exceptions=False)
    c.headers[ws._SESSION_HEADER_NAME] = ws._SESSION_TOKEN
    yield c
    busy.reset()


def test_fresh_memo_lifecycle_busy_returns_retryable_503(client, monkeypatch, endpoint):
    def held(*args):
        raise BoardLifecycleBusy()

    monkeypatch.setattr(ksa, "_read_session_tasks", held)
    response = client.get(endpoint, params={"ids": "s-cold"})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json() == {"detail": "Kanban board is busy; retry shortly."}
    assert not busy._tasks_memo


def test_busy_memo_still_returns_stale_rows(client, monkeypatch, endpoint):
    rows = [{"id": "t-cached", "board": "default", "status": "running"}]
    monkeypatch.setattr(ksa, "_read_session_tasks", lambda *a: rows)
    assert client.get(endpoint, params={"ids": "s-warm"}).status_code == 200

    def held(*args):
        raise BoardLifecycleBusy()

    monkeypatch.setattr(ksa, "_read_session_tasks", held)
    response = client.get(endpoint, params={"ids": "s-warm"})
    assert response.status_code == 200
    assert response.json()["tasks"] == [{**rows[0], "stale": True}]


def test_non_busy_database_error_is_not_mislabeled(client, monkeypatch, endpoint):
    def broken(*args):
        raise sqlite3.OperationalError("no such table: tasks")

    monkeypatch.setattr(ksa, "_read_session_tasks", broken)
    response = client.get(endpoint, params={"ids": "s-broken"})
    assert response.status_code == 500
    assert "retry-after" not in response.headers
