"""Opt-in Cloudflare Access trust in the dashboard gate (``dashboard_auth.cf_access``)."""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import cf_access, clear_providers, register_provider
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider

TEAM = "team.cloudflareaccess.com"
AUD = "aud-mini"
EMAIL = "me@example.com"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _StubJwks:
    def get_signing_key_from_jwt(self, token):
        class _K:
            key = _KEY.public_key()
        return _K()


def _token(*, key=_KEY, aud=AUD, iss=f"https://{TEAM}", email=EMAIL, exp_in=600):
    now = int(time.time())
    claims = {"aud": [aud], "iss": iss, "sub": "user-1", "iat": now, "exp": now + exp_in}
    if email is not None:
        claims["email"] = email
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def gated(monkeypatch):
    clear_providers()
    register_provider(StubAuthProvider())
    monkeypatch.setattr(cf_access, "_jwks_client", lambda url: _StubJwks())
    monkeypatch.setenv("HERMES_DASHBOARD_CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("HERMES_DASHBOARD_CF_ACCESS_AUD", f"other-aud, {AUD}")
    monkeypatch.setenv("HERMES_DASHBOARD_CF_ACCESS_EMAILS", "Me@Example.com")
    saved = {k: getattr(web_server.app.state, k, None) for k in ("bound_host", "bound_port", "auth_required")}
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    yield TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    clear_providers()
    for k, v in saved.items():
        setattr(web_server.app.state, k, v)


def _me(client, token=None):
    headers = {cf_access.HEADER: token} if token else {}
    return client.get("/api/auth/me", headers=headers)


def test_valid_allowlisted_access_jwt_is_a_session(gated):
    r = _me(gated, _token())
    assert r.status_code == 200
    assert r.json()["email"] == EMAIL
    assert r.json()["provider"] == "cloudflare_access"


def test_ws_ticket_works_with_access_session(gated):
    r = gated.post("/api/auth/ws-ticket", headers={cf_access.HEADER: _token()})
    assert r.status_code == 200 and r.json()["ticket"]


@pytest.mark.parametrize("kwargs", [
    {"aud": "some-other-app"},
    {"iss": "https://evil.cloudflareaccess.com"},
    {"email": "someone@else.com"},
    {"email": None},  # Access service token: no email claim -> normal login (Redde)
    {"key": _OTHER_KEY},
    {"exp_in": -3600},
])
def test_rejected_assertions_fall_through_to_normal_gate(gated, kwargs):
    r = _me(gated, _token(**kwargs))
    assert r.status_code == 401
    assert r.json()["error"] == "unauthenticated"


def test_disabled_without_full_config(gated, monkeypatch):
    monkeypatch.delenv("HERMES_DASHBOARD_CF_ACCESS_EMAILS")
    assert _me(gated, _token()).status_code == 401


def test_no_header_still_requires_login(gated):
    assert _me(gated).status_code == 401


def test_login_page_bounces_back_when_access_session_valid(gated):
    r = gated.get("/login", params={"next": "/sessions?x=1"}, headers={cf_access.HEADER: _token()},
                  follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/sessions?x=1"
    r = gated.get("/login", params={"next": "//evil.com"}, headers={cf_access.HEADER: _token()},
                  follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"


def test_login_page_renders_without_access_session(gated):
    r = gated.get("/login", headers={cf_access.HEADER: _token(email="x@y.z")})
    assert r.status_code == 200


def test_load_config_requires_all_settings():
    assert cf_access.load_config({}) is None
    cfg = cf_access.load_config({
        "HERMES_DASHBOARD_CF_ACCESS_TEAM_DOMAIN": "https://Team.cloudflareaccess.com/",
        "HERMES_DASHBOARD_CF_ACCESS_AUD": "a,b",
        "HERMES_DASHBOARD_CF_ACCESS_EMAILS": "A@B.c"})
    assert cfg and cfg.team_domain == "team.cloudflareaccess.com"
    assert cfg.audiences == ("a", "b") and cfg.emails == frozenset({"a@b.c"})
