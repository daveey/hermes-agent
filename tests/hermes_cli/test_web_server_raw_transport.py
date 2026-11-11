"""Transport observation must precede Uvicorn's trusted proxy rewrite, not replace it."""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


@pytest.mark.parametrize("builder", ["listener", "dashboard"])
@pytest.mark.parametrize("proxy_enabled", [False, True])
def test_builders_preserve_raw_peer_without_changing_proxy_semantics(monkeypatch, builder, proxy_enabled):
    from hermes_cli.web_server_lifecycle import build_listener_server

    app = FastAPI()
    app.state.auth_required = proxy_enabled

    @app.get("/observe")
    def observe(request: Request):
        return {"raw": request.scope.get("hermes.raw_transport"),
                "client": request.scope["client"], "scheme": request.scope["scheme"]}

    if builder == "listener":
        config, _ = build_listener_server(app, "127.0.0.1", 0, auth_required=proxy_enabled)
    else:
        from hermes_cli import web_server
        monkeypatch.setattr(web_server, "app", app)
        config, _ = web_server._build_uvicorn_server("127.0.0.1", 0)
    config.load()
    response = TestClient(config.loaded_app, client=("127.0.0.1", 40123)).get(
        "http://127.0.0.1:8765/observe", headers={"X-Forwarded-For": "192.0.2.99", "X-Forwarded-Proto": "https"})
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["raw"] == [["127.0.0.1", 40123], ["127.0.0.1", 8765]]
    assert value["client"] == (["192.0.2.99", 0] if proxy_enabled else ["127.0.0.1", 40123])
    assert value["scheme"] == ("https" if proxy_enabled else "http")
