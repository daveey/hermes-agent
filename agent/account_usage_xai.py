"""xAI (Grok) subscription usage for ``xai-oauth`` logins. [daveey]

The Grok subscription (SuperGrok / X Premium / X Ultra) has one shared weekly usage pool across Chat, Build and the
API. It has no public REST doc, and ``api.x.ai`` response headers carry only per-minute RPS/TPM limits. The official
Grok Build CLI reads the pool from ``GET https://cli-chat-proxy.grok.com/v1/billing?format=credits`` with the OAuth
bearer (xai-org/grok-build ``xai-grok-shell/src/extensions/billing.rs``):

    {"config": {"currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "start": "...", "end": "..."},
                "creditUsagePercent": 2.0, "productUsage": [{"product": "Api", "usagePercent": 2.0}], ...}}

Live read 2026-11-15 (hub X Ultra row): HTTP 200, weekly period, 2%. The weekly pool maps to one account-scope
``Current week`` window, so the Kanban pacing governor judges ``xai-oauth`` like the Claude/Codex pools (t_01a1e076).
Any other period type yields no window (``unavailable_reason``): the governor's 5h/7d math cannot place it, and an
unknown pool fails open rather than being mis-paced.

Only an xAI OAuth token is ever sent to this host (never an ``XAI_API_KEY``), redirects are not followed, and a read
never refreshes or rotates a pool credential when called with an explicit token.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
WEEKLY = "USAGE_PERIOD_TYPE_WEEKLY"
SOURCE = "grok_cli_billing"
TIMEOUT_SECONDS = 15.0


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json", "X-XAI-Token-Auth": "xai-grok-cli",
            "User-Agent": "hermes-agent"}


def _percent(value: Any) -> Optional[float]:
    from agent.account_usage import _is_finite_num
    return max(0.0, min(100.0, float(value))) if _is_finite_num(value) and not isinstance(value, bool) else None


def snapshot_from_payload(payload: Any):
    """``/v1/billing?format=credits`` body -> :class:`~agent.account_usage.AccountUsageSnapshot`."""
    from agent.account_usage import AccountUsageWindow, _parse_dt, _snapshot
    config = payload.get("config") if isinstance(payload, dict) else None
    config = config if isinstance(config, dict) else {}
    found = config.get("currentPeriod")
    period: dict = found if isinstance(found, dict) else {}
    kind = str(period.get("type") or "")
    used = _percent(config.get("creditUsagePercent"))
    details = []
    for row in config.get("productUsage") or []:
        share = _percent(row.get("usagePercent")) if isinstance(row, dict) else None
        if share is not None and row.get("product"):
            details.append(f"{row['product']}: {share:.0f}% of the weekly pool")
    raw = payload if isinstance(payload, dict) else None
    if kind != WEEKLY or used is None:
        reason = (f"xAI usage period {kind or 'missing'} is not a weekly window" if kind != WEEKLY
                  else "xAI usage response carries no creditUsagePercent")
        return _snapshot("xai-oauth", SOURCE, [], details, unavailable_reason=reason, raw=raw)
    reset = _parse_dt(period.get("end")) or _parse_dt(config.get("billingPeriodEnd"))
    window = AccountUsageWindow(label="Current week", used_percent=used, reset_at=reset, scope="account")
    return _snapshot("xai-oauth", SOURCE, [window], details, title="Grok subscription", raw=raw)


def fetch(api_key: Optional[str], *, read_only: bool = False):
    """Usage of the xAI OAuth login ``api_key`` (else the resolved runtime login unless ``read_only``)."""
    token = str(api_key or "").strip()
    if not token and not read_only:
        from hermes_cli.auth_xai import resolve_xai_oauth_runtime_credentials
        creds = resolve_xai_oauth_runtime_credentials() or {}
        token = str(creds.get("api_key") or creds.get("access_token") or "").strip()
    if not token:
        return None
    with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
        response = client.get(BILLING_URL, headers=_headers(token))
        response.raise_for_status()
    return snapshot_from_payload(response.json() or {})
