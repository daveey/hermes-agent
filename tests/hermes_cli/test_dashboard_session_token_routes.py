"""Core service calls that authenticate with ``X-Hermes-Session-Token`` must pass a gated dashboard.

``hermes plugins enable`` posts ``/api/dashboard/agent-plugins/activate`` and the review-retire path
reads ``/api/health/idle`` / posts ``/api/health/retirement`` with the deployment session token and
no cookie. On a gated dashboard (``auth_required``) the cookie gate refused them as ``no_cookie``
before the handler ran, so plugin activation never reached open Desktop chats. These paths are
registered as session-token routes on the token seam; every other path keeps the cookie gate.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth import token_auth
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider

REPO = Path(__file__).resolve().parents[2]
HEADER = "X-Hermes-Session-Token"


@pytest.fixture
def gated(monkeypatch):
    clear_providers()
    register_provider(StubAuthProvider())
    state = web_server.app.state
    prev = {k: getattr(state, k, None) for k in ("bound_host", "bound_port", "auth_required")}
    state.bound_host, state.bound_port, state.auth_required = "fly-app.fly.dev", 443, True

    import hermes_cli.plugins_activation as pa
    import hermes_cli.web_server_idle_proof as idle
    from hermes_cli.backend_retirement import retirement
    monkeypatch.setattr(pa, "load_and_go_live", lambda name: {"name": name, "activated_now": {"hooks": 1}})
    monkeypatch.setattr(idle, "idle_proof", lambda: {"idle": True})
    monkeypatch.setattr(retirement, "prepare", lambda: {"ok": True, "prepared": True})
    yield TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    clear_providers()
    for k, v in prev.items():
        setattr(state, k, v)


def _activate(client, headers):
    from hermes_constants import get_default_hermes_root
    return client.post("/api/dashboard/agent-plugins/activate", headers=headers,
                       json={"name": "probe", "home": str(get_default_hermes_root())})


CALLS = {
    "activate": _activate,
    "idle": lambda c, h: c.get("/api/health/idle", headers=h),
    "retirement": lambda c, h: c.post("/api/health/retirement", headers=h, json={"action": "prepare"}),
}


@pytest.mark.parametrize("route", sorted(CALLS))
def test_valid_session_token_without_cookie_passes_the_gate(gated, route):
    resp = CALLS[route](gated, {HEADER: web_server._SESSION_TOKEN})
    assert resp.status_code == 200, resp.text
    assert resp.json().get("ok") is True


@pytest.mark.parametrize("route", sorted(CALLS))
def test_wrong_session_token_is_refused(gated, route):
    resp = CALLS[route](gated, {HEADER: "not-the-token"})
    assert resp.status_code == 401


@pytest.mark.parametrize("route", sorted(CALLS))
def test_no_token_is_refused(gated, route):
    resp = CALLS[route](gated, {})
    assert resp.status_code == 401


def test_unregistered_require_token_route_keeps_the_cookie_gate(gated):
    """The gate is not widened: a header-only call to a route nobody registered still 401s."""
    assert not token_auth.is_token_route("/api/env/reveal")
    resp = gated.post("/api/env/reveal", headers={HEADER: web_server._SESSION_TOKEN}, json={"key": "X"})
    assert resp.status_code == 401
    assert resp.json().get("reason") == "no_cookie"


def test_session_token_routes_survive_the_test_only_clear():
    token_auth.clear_token_routes()
    for path in web_server.SESSION_TOKEN_SERVICE_ROUTES:
        assert token_auth.is_session_token_route(path)


def test_loopback_dashboard_still_accepts_the_session_header(gated):
    """Control: the ungated (loopback) path is unchanged."""
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app, base_url="http://127.0.0.1")
    web_server.app.state.bound_host = "127.0.0.1"
    assert client.get("/api/health/idle", headers={HEADER: web_server._SESSION_TOKEN}).status_code == 200
    assert client.get("/api/health/idle", headers={HEADER: "nope"}).status_code == 401


# ---------------------------------------------------------------------------
# Recurrence guard: every core caller that sends the session header to a dashboard path must
# target a registered session-token route, or a gated dashboard refuses it as no_cookie.
# ---------------------------------------------------------------------------

# In-process callers that never cross the gate, with the reason.
_EXEMPT = {
    ("hermes_cli/web_server.py", "_dashboard_selftest_once"):
        "in-process ASGI self-test; its loop skips gated binds",
    ("hermes_cli/always_on_rollover.py", "_http_get"):
        "roller -> its own loopback-only always-on backend (serve --isolated on 127.0.0.1, never gated); "
        "the path is a parameter because warmup replays Desktop's own GETs",
    ("hermes_cli/always_on_rollover.py", "_http_post"):
        "roller -> its own loopback-only always-on backend (never gated); the path is a parameter "
        "(only /api/health/retirement, already a session-token route)",
    ("hermes_cli/always_on_rollover.py", "_loopback_control_get"):
        "superseded always-on backend -> the active successor on 127.0.0.1 (serve --isolated, never gated); "
        "the path is a parameter because CONTROL_READ_PATHS is an allowlist, not a gated-dashboard route",
}
_API_PATH = re.compile(r"/api/[A-Za-z0-9_\-/{}.]*")


def _sends_session_header(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "headers" and isinstance(kw.value, ast.Dict):
            for key in kw.value.keys:
                if isinstance(key, ast.Constant) and key.value == HEADER:
                    return True
                if isinstance(key, ast.Name) and key.id == "_SESSION_HEADER_NAME":
                    return True
    return False


def _session_header_senders():
    found = []
    for top in ("hermes_cli", "gateway"):
        for path in sorted((REPO / top).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            consts = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
                      and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
                      for t in n.targets if isinstance(t, ast.Name)}
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not any(isinstance(c, ast.Call) and _sends_session_header(c) for c in ast.walk(fn)):
                    continue
                texts = [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
                texts += [consts[n.id] for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id in consts]
                paths = {m.split("?")[0] for t in texts for m in _API_PATH.findall(t)}
                found.append((str(path.relative_to(REPO)), fn.name, paths))
    return found


def test_every_session_header_caller_targets_a_session_token_route():
    senders = _session_header_senders()
    names = {(f, fn) for f, fn, _ in senders}
    # Not vacuous: the two known service callers are found.
    assert ("hermes_cli/plugins_activation.py", "notify_serve_backend_record") in names
    assert ("hermes_cli/dashboard_procs.py", "_retirement_request") in names
    for file, fn, paths in senders:
        if (file, fn) in _EXEMPT:
            continue
        assert paths, f"{file}:{fn} sends {HEADER} but its target path could not be read"
        missing = sorted(p for p in paths if not token_auth.is_session_token_route(p))
        assert not missing, (
            f"{file}:{fn} sends {HEADER} to {missing}, which a gated dashboard refuses as no_cookie; "
            "add the path to web_server.SESSION_TOKEN_SERVICE_ROUTES")
