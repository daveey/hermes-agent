"""xai-oauth (Grok subscription) in the Kanban LLM pacing governor (t_01a1e076d10286f1). [daveey]

Before: no usage reader existed for ``xai-oauth`` and it was not a governed provider, so Grok cards (and every
``class:`` card whose routes include Grok) started ungoverned. The Grok subscription has one weekly usage pool, read
by the Grok Build CLI from ``cli-chat-proxy.grok.com/v1/billing?format=credits``; it now maps to a ``seven_day``
account window and ``xai-oauth`` is governed by default.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from agent import account_usage as au
from agent import account_usage_xai as ax
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd
from tests.hermes_cli.test_kanban_llm_pacing_followups import CFG, DAY, _config, _tick, paced_board
from tests.hermes_cli.test_kanban_model_tier import board as _board_fixture

board = _board_fixture
__all__ = ["paced_board"]

END = "2026-11-16T22:52:31.914+00:00"


def _payload(used: float | None = 2.0, kind: str = ax.WEEKLY, end: str = END) -> dict:
    """Shape of the live hub read, 2026-11-15 (X Ultra)."""
    return {"config": {"currentPeriod": {"type": kind, "start": "2026-11-09T22:52:31.914+00:00", "end": end},
                       "creditUsagePercent": used, "productUsage": [{"product": "Api", "usagePercent": used},
                                                                    {"product": "GrokChat"}],
                       "isUnifiedBillingUser": True, "prepaidBalance": {"val": 0},
                       "billingPeriodStart": "2026-11-09T22:52:31.914+00:00", "billingPeriodEnd": end}}


# --- the reader -------------------------------------------------------------------------------------------------

def test_weekly_credit_pool_maps_to_one_account_week_window():
    snap = ax.snapshot_from_payload(_payload(2.0))
    assert snap.available and snap.provider == "xai-oauth"
    (window,) = snap.windows
    assert (window.label, window.used_percent, window.scope) == ("Current week", 2.0, "account")
    assert window.reset_at == datetime(2026, 11, 16, 22, 52, 31, 914000, tzinfo=UTC)
    assert lp.window_kind(window.label) == lp.SEVEN_DAY
    assert snap.details == ("Api: 2% of the weekly pool",)  # a product row without a percent is skipped


@pytest.mark.parametrize("payload", [_payload(kind="USAGE_PERIOD_TYPE_MONTHLY"), _payload(used=None), {}, {"config": []},
                                     # the legacy shape (no ``format=credits``): monthlyLimit/used, no period
                                     {"config": {"monthlyLimit": {"val": 0}, "used": {"val": 0}}}])
def test_anything_but_a_weekly_percent_is_unavailable_never_headroom(payload):
    snap = ax.snapshot_from_payload(payload)
    assert not snap.available and snap.windows == () and snap.unavailable_reason


def _client_spy(monkeypatch, status=200, body=None):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=_payload() if body is None else body,
                              headers={"location": "https://evil.example/"} if 300 <= status < 400 else {})
    real = httpx.Client
    monkeypatch.setattr(ax.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen


def test_fetch_account_usage_dispatches_xai_oauth_to_the_grok_billing_read(monkeypatch):
    seen = _client_spy(monkeypatch)
    snap = au.fetch_account_usage("xai-oauth", api_key="oauth-token", read_only=True, identity_id="acct")
    assert snap is not None and snap.windows[0].used_percent == 2.0
    (request,) = seen
    assert str(request.url) == "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
    assert request.headers["authorization"] == "Bearer oauth-token"
    assert request.headers["x-xai-token-auth"] == "xai-grok-cli"


def test_a_redirect_is_not_followed_with_the_bearer(monkeypatch):
    seen = _client_spy(monkeypatch, status=302)
    assert au.fetch_account_usage("xai-oauth", api_key="oauth-token", read_only=True) is None
    assert len(seen) == 1


def test_read_only_without_an_explicit_token_never_resolves_a_credential(monkeypatch):
    from hermes_cli import auth_xai
    monkeypatch.setattr(auth_xai, "resolve_xai_oauth_runtime_credentials",
                        lambda *a, **k: pytest.fail("read-only usage read must not refresh/resolve a login"))
    seen = _client_spy(monkeypatch)
    assert au.fetch_account_usage("xai-oauth", read_only=True) is None and seen == []


# --- the governor ----------------------------------------------------------------------------------------------

def test_xai_oauth_is_governed_by_default():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert "xai-oauth" in lp.PacingConfig().providers
    assert "xai-oauth" in lp.config_from(DEFAULT_CONFIG["kanban"]["llm_pacing"]).providers


def _xai_pool(monkeypatch, used, end_epoch):
    end = datetime.fromtimestamp(end_epoch, tz=UTC).isoformat()
    monkeypatch.setattr(pd, "_snapshots", lambda provider: [("X Ultra (@daveey)", ax.snapshot_from_payload(
        _payload(used, end=end)))] if provider == "xai-oauth" else [])


def test_read_pool_judges_the_xai_pool_instead_of_unknown(monkeypatch):
    now = 1_800_000_000.0
    _xai_pool(monkeypatch, 2.0, now + 1.5 * DAY)
    current = pd.read_pool("xai-oauth", now)
    assert [(r.account, r.kind, r.used, r.reset_at) for r in current] == [
        ("X Ultra (@daveey)", lp.SEVEN_DAY, 2.0, pytest.approx(now + 1.5 * DAY))]
    assert lp.pool_pace("xai-oauth", current, [], CFG).state == lp.OPEN

    _xai_pool(monkeypatch, 90.0, now + 1.5 * DAY)  # past the 85% weekly reserve: held until the reset
    pace = lp.pool_pace("xai-oauth", pd.read_pool("xai-oauth", now), [], CFG)
    assert (pace.state, pace.until) == (lp.HELD, pytest.approx(now + 1.5 * DAY))


def test_a_grok_card_is_held_when_the_grok_pool_is_past_its_reserve(paced_board, monkeypatch):
    import time
    # no llm_pacing override: the defaults must govern xai-oauth; the default class resolves to the Grok pool only
    _config(paced_board, model_routing={"enabled": True, "routes": {"standard": [
        {"provider": "xai-oauth", "model": "grok-4.6", "billing": "subscription", "weight": 1, "eligible": True,
         "funding_verified": True, "capabilities": {"tools": True, "streaming": True, "context_tokens": 1000000}}]}})
    _xai_pool(monkeypatch, 90.0, time.time() + DAY)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="grok work", assignee="default")
        seen, _ = _tick(conn)
        assert tid not in seen
        events = [e.payload for e in kb.list_events(conn, tid) if e.kind == pd.EVENT_KIND]
    assert events and events[-1] and [(p["provider"], p["state"], p["limit"]) for p in events[-1]["providers"]] == [
        ("xai-oauth", lp.HELD, 0)]
    stored = json.loads(pd.state_path().read_text(encoding="utf-8"))
    assert stored["xai-oauth"]["pace"]["state"] == lp.HELD
