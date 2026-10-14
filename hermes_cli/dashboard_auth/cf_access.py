"""Trust a Cloudflare Access login as a dashboard session (opt-in, off by default).

When the dashboard sits behind a Cloudflare Access application, every proxied request carries a
signed ``Cf-Access-Jwt-Assertion`` header. If the operator configures the team domain, the
application's AUD tag(s) and an email allowlist, a request whose JWT verifies (RS256 against the
team's ``/cdn-cgi/access/certs``, pinned ``iss`` + ``aud``) and whose ``email`` claim is allowed is
treated as an authenticated session, so the user isn't asked to log in a second time.

Fails closed: any missing setting disables it; a JWT without an ``email`` claim (an Access
*service token*) is ignored, so machine clients keep using the normal provider login; an
unreachable certs endpoint or a bad token just falls through to the normal gate (never 503).

Settings (environment, per host):
  HERMES_DASHBOARD_CF_ACCESS_TEAM_DOMAIN  e.g. ``myteam.cloudflareaccess.com``
  HERMES_DASHBOARD_CF_ACCESS_AUD          comma-separated Access application AUD tag(s)
  HERMES_DASHBOARD_CF_ACCESS_EMAILS       comma-separated allowed emails (case-insensitive)
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from fastapi import Request

from hermes_cli.dashboard_auth.base import Session

_log = logging.getLogger(__name__)

HEADER = "cf-access-jwt-assertion"
PROVIDER_NAME = "cloudflare_access"
JWKS_CACHE_SECONDS = 3600
LEEWAY_SECONDS = 60


@dataclass(frozen=True)
class CfAccessConfig:
    team_domain: str
    audiences: tuple[str, ...]
    emails: frozenset[str]

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    @property
    def certs_url(self) -> str:
        return f"https://{self.team_domain}/cdn-cgi/access/certs"


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def load_config(env: Optional[Mapping[str, str]] = None) -> Optional[CfAccessConfig]:
    """The configured trust, or ``None`` (disabled) unless all three settings are present."""
    src: Mapping[str, str] = os.environ if env is None else env
    team = src.get("HERMES_DASHBOARD_CF_ACCESS_TEAM_DOMAIN", "").strip().lower()
    team = team.removeprefix("https://").rstrip("/")
    auds = tuple(_split(src.get("HERMES_DASHBOARD_CF_ACCESS_AUD", "")))
    emails = frozenset(e.lower() for e in _split(src.get("HERMES_DASHBOARD_CF_ACCESS_EMAILS", "")))
    if not (team and auds and emails):
        return None
    return CfAccessConfig(team_domain=team, audiences=auds, emails=emails)


_jwks_lock = threading.Lock()
_jwks_clients: dict[str, Any] = {}


def _jwks_client(url: str) -> Any:
    with _jwks_lock:
        client = _jwks_clients.get(url)
        if client is None:
            from jwt import PyJWKClient

            client = PyJWKClient(
                url, cache_keys=True, lifespan=JWKS_CACHE_SECONDS,
                headers={"Accept": "application/json", "User-Agent": "HermesAgent/1.0"})
            _jwks_clients[url] = client
        return client


def verify_assertion(token: str, cfg: CfAccessConfig) -> Optional[Session]:
    """Session for a valid, allowed Access JWT; ``None`` for anything else. Never raises."""
    import jwt

    try:
        key = _jwks_client(cfg.certs_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token, key.key, algorithms=["RS256"], audience=list(cfg.audiences), issuer=cfg.issuer,
            leeway=LEEWAY_SECONDS, options={"require": ["exp", "iat", "aud", "iss", "sub"]})
    except Exception as exc:  # bad/expired/foreign token or certs unreachable: normal login applies
        _log.info("dashboard-auth: Cloudflare Access assertion not accepted: %s", exc)
        return None
    email = str(claims.get("email") or "").strip().lower()
    if not email or email not in cfg.emails:
        # Service tokens carry no email; other identities aren't on the allowlist.
        return None
    return Session(
        user_id=str(claims["sub"]), email=email, display_name=email, org_id="",
        provider=PROVIDER_NAME, expires_at=int(claims["exp"]), access_token="", refresh_token="")


def session_from_request(request: Request) -> Optional[Session]:
    """Verified Session from the request's Access header when the trust is configured."""
    token = request.headers.get(HEADER, "").strip()
    if not token:
        return None
    cfg = load_config()
    if cfg is None:
        return None
    return verify_assertion(token, cfg)
