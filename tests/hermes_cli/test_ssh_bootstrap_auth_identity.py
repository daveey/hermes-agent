"""Only validated SSH Desktop startup credentials confer a stable principal."""
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket

from hermes_cli import web_server as server
from hermes_cli.web_server_chat import _ws_auth_reason
from hermes_cli.main_dashboard import _read_ssh_session_token_file


@pytest.fixture(autouse=True)
def state(monkeypatch):
    monkeypatch.setattr(server, "_SESSION_TOKEN", "b" * 64)
    monkeypatch.setattr(server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(server.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(server.app.state, "ssh_bootstrap_identity", None, raising=False)


def bootstrap(owner="a" * 32, token="b" * 64, host="127.0.0.1"):
    server._configure_ssh_bootstrap_identity(host, token, owner)


async def rest(token):
    request = Request({"type": "http", "method": "POST", "path": "/api/sessions/start",
                       "headers": [(b"x-hermes-session-token", token.encode())],
                       "query_string": b"user_id=attacker&provider=attacker", "app": server.app})
    async def downstream(request):
        session = getattr(request.state, "session", None)
        return JSONResponse({"identity": None if session is None else
                             {"provider": session.provider, "user_id": session.user_id}})
    response = await server.auth_middleware(request, downstream)
    import json
    return response.status_code, json.loads(response.body)


def ws(token):
    socket = WebSocket({"type": "websocket", "path": "/api/ws", "headers": [],
                        "query_string": f"token={token}&user_id=attacker".encode(),
                        "app": server.app, "scheme": "ws", "server": ("127.0.0.1", 9119)}, receive=None, send=None)
    reason, _ = _ws_auth_reason(socket)
    return reason, getattr(socket, "_hermes_auth_identity", None)


@pytest.mark.asyncio
async def test_actual_rest_ws_identity_parity():
    bootstrap()
    expected = {"provider": "desktop-ssh", "user_id": "a" * 32}
    assert await rest("b" * 64) == (200, {"identity": expected})
    assert ws("b" * 64) == (None, expected)


@pytest.mark.asyncio
async def test_legacy_no_bootstrap_has_no_identity():
    assert await rest("b" * 64) == (200, {"identity": None})
    assert ws("b" * 64) == (None, None)


@pytest.mark.asyncio
async def test_wrong_or_missing_token_has_no_identity():
    bootstrap()
    for token in ("wrong", ""):
        assert (await rest(token))[0] == 401
        assert ws(token)[0] is not None
        assert ws(token)[1] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["0.0.0.0", "example.com", ""])
async def test_nonloopback_bootstrap_has_no_identity(host):
    bootstrap(host=host)
    assert await rest("b" * 64) == (200, {"identity": None})
    assert ws("b" * 64) == (None, None)


@pytest.mark.asyncio
async def test_gating_never_uses_ssh_identity(monkeypatch):
    bootstrap()
    monkeypatch.setattr(server.app.state, "auth_required", True)
    monkeypatch.setattr("hermes_cli.dashboard_auth.middleware._verify_access_token", lambda *a, **k: None)
    assert await rest("b" * 64) == (200, {"identity": None})  # outer OAuth gate owns rejection
    assert ws("b" * 64) == ("token_invalid", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,token", [(None, "b" * 64), ("a" * 32, None),
                                         ("attacker", "b" * 64)])
async def test_incomplete_or_malformed_startup_has_no_identity(owner, token):
    bootstrap(owner=owner, token=token)
    assert await rest("b" * 64) == (200, {"identity": None})
    assert ws("b" * 64) == (None, None)


@pytest.mark.asyncio
async def test_startup_reset_and_token_replacement(monkeypatch):
    bootstrap()
    bootstrap(owner=None, token=None)
    assert ws("b" * 64) == (None, None)
    bootstrap()
    monkeypatch.setattr(server, "_SESSION_TOKEN", "c" * 64)
    assert await rest("c" * 64) == (200, {"identity": None})
    assert ws("c" * 64) == (None, None)


@pytest.mark.platforms("posix")
def test_validated_ownership_stable_across_nonce_and_token_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    identities = []
    for owner, nonce, token in [("a" * 32, "1" * 16, "b" * 64),
                                ("a" * 32, "2" * 16, "c" * 64),
                                ("d" * 32, "3" * 16, "e" * 64)]:
        directory = tmp_path / ".hermes" / "desktop-ssh" / owner
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (nonce + ".token")
        path.write_text(token)
        path.chmod(0o600)
        validated = _read_ssh_session_token_file(str(path))
        assert not path.exists()
        monkeypatch.setattr(server, "_SESSION_TOKEN", validated)
        bootstrap(owner=path.parent.name, token=validated)
        identities.append(ws(validated)[1])
    assert identities[0] is not None
    assert identities[0] == identities[1]
    assert identities[0] != identities[2]


def test_asgi_middleware_and_websocket_upgrade_parity():
    bootstrap()
    app = FastAPI()
    app.middleware("http")(server.auth_middleware)

    @app.post("/api/probe")
    async def probe(request: Request):
        session = request.state.session
        return {"provider": session.provider, "user_id": session.user_id}

    @app.websocket("/api/ws")
    async def socket(ws: WebSocket):
        reason, _ = _ws_auth_reason(ws)
        if reason:
            await ws.close(code=1008)
            return
        await ws.accept()
        await ws.send_json(ws._hermes_auth_identity)
        await ws.close()

    client = TestClient(app)
    response = client.post("/api/probe", headers={"X-Hermes-Session-Token": "b" * 64})
    assert response.status_code == 200
    with client.websocket_connect("/api/ws?token=" + "b" * 64) as connection:
        assert connection.receive_json() == response.json()
    assert client.post("/api/probe", headers={"X-Hermes-Session-Token": "wrong"}).status_code == 401


def test_start_server_resets_binding_on_each_start(monkeypatch):
    class StopStartup(Exception):
        pass

    def stop(*args):
        raise StopStartup

    monkeypatch.setattr(server, "_apply_ssh_owner_nonce", stop)
    with pytest.raises(StopStartup):
        server.start_server(ssh_session_token="b" * 64, ssh_ownership_id="a" * 32)
    assert ws("b" * 64)[1] == {"provider": "desktop-ssh", "user_id": "a" * 32}
    with pytest.raises(StopStartup):
        server.start_server()
    assert ws("b" * 64) == (None, None)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("safe", [True, False])
def test_cmd_dashboard_only_passes_ownership_after_file_validation(tmp_path, monkeypatch, safe):
    # Bootstrap recovery resolves worktree git metadata outside HERMES_HOME;
    # this command-wiring test must never inspect or recover the live checkout.
    monkeypatch.setattr("hermes_cli._early_recovery.restore_interrupted_pull", lambda *a: False)
    from hermes_cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    directory = tmp_path / ".hermes" / "desktop-ssh" / ("a" * 32)
    directory.mkdir(parents=True, mode=0o700)
    path = directory / ("1" * 16 + ".token")
    path.write_text("b" * 64)
    path.chmod(0o600 if safe else 0o644)
    for name in ("_dashboard_lifecycle_flags", "_dashboard_validate_serve_args",
                 "_dashboard_sanitize_desktop_env", "_attach_to_host_backend",
                 "_route_named_profile_dashboard", "_dashboard_prepare_runtime",
                 "_maybe_setup_dashboard_auth_interactively"):
        monkeypatch.setattr(main, name, lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.resource_limits.apply_nofile_soft_limit", lambda: None)
    calls = []
    monkeypatch.setattr(server, "start_server", lambda **kwargs: calls.append(kwargs))
    args = SimpleNamespace(ssh_session_token_file=str(path), host="127.0.0.1", port=9119, no_open=True)
    if not safe:
        with pytest.raises(SystemExit, match="unsafe permissions"):
            main.cmd_dashboard(args)
        assert not calls
    else:
        main.cmd_dashboard(args)
        assert calls[0]["ssh_ownership_id"] == "a" * 32
        assert calls[0]["ssh_session_token"] == "b" * 64
        assert not path.exists()
