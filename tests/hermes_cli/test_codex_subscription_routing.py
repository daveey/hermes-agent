"""Astra must route via the ChatGPT/Codex subscription, never silently via the metered OpenAI API.

Regression: the Codex catalog was discovered with whichever pool entry round-robin pointed at; a
free-plan entry lists no Astra, so the picker offered it only under the billed OpenAI API row.
"""
from __future__ import annotations

import base64
import json
import time
from unittest.mock import patch

import pytest

import hermes_cli.auth as auth_mod
from hermes_cli import codex_models
from agent.credential_pool import (
    codex_entry_entitled_for_model, codex_plan_type, load_pool)
from hermes_cli.inventory import _prefer_subscription_rows

ASTRA = "gpt-6-astra"


def _jwt(account: str, plan: str) -> str:
    def seg(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    payload = {"exp": int(time.time()) + 3600, "sub": account,
               "https://api.openai.com/auth": {"chatgpt_account_id": account, "chatgpt_plan_type": plan}}
    return f"{seg({'alg': 'none'})}.{seg(payload)}.sig"


FREE, PRO = _jwt("acct-free", "free"), _jwt("acct-pro", "pro")


@pytest.fixture
def pool_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    (home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"openai-codex": [
        {"id": "free1", "label": "free", "auth_type": "oauth", "priority": 0, "source": "manual:device_code",
         "access_token": FREE, "refresh_token": "r1"},
        {"id": "pro1", "label": "pro", "auth_type": "oauth", "priority": 1, "source": "manual:device_code",
         "access_token": PRO, "refresh_token": "r2"}]}}), encoding="utf-8")
    return home


def test_plan_claim_and_entitlement():
    assert codex_plan_type(FREE) == "free" and codex_plan_type(PRO) == "pro"
    assert not codex_entry_entitled_for_model(FREE, ASTRA)
    assert codex_entry_entitled_for_model(PRO, ASTRA)
    assert codex_entry_entitled_for_model(FREE, "gpt-5.5")  # only gated models are plan-checked
    assert codex_entry_entitled_for_model("opaque-token", ASTRA)  # unknown plan is not excluded


def test_pool_selection_for_astra_skips_free_entries(pool_home):
    pool = load_pool("openai-codex")
    for _ in range(4):  # the free head entry is never leased for Astra
        assert pool.select(model=ASTRA).label == "pro"
    assert pool.select(model="gpt-5.5").label == "free"  # ungated models still use the head entry


def test_codex_catalog_unions_all_pool_entries_when_current_is_free(pool_home):
    def fake_fetch(token, base_url=None):
        return ["gpt-5.5", ASTRA] if token == PRO else ["gpt-5.5", "luna"]

    # the read-only singleton resolver points at the FREE entry (the round-robin cursor)
    with patch.object(auth_mod, "resolve_codex_runtime_credentials",
                      return_value={"api_key": FREE, "base_url": None}), \
         patch.object(codex_models, "_fetch_models_from_api", side_effect=fake_fetch):
        from hermes_cli.models import _codex_catalog

        models = _codex_catalog("openai-codex", False)
        creds = codex_models.codex_catalog_credentials()
    assert ASTRA in models and "luna" in models
    assert codex_plan_type(creds[0][0]) == "pro"  # strongest plan first


def test_catalog_cache_identity_ignores_cursor(pool_home):
    def ident(current):
        with patch.object(auth_mod, "resolve_codex_runtime_credentials",
                          return_value={"api_key": current, "base_url": None}):
            return codex_models.codex_catalog_credential_identity()

    assert ident(FREE) == ident(PRO)


def _row(slug, models):
    return {"slug": slug, "name": slug, "models": list(models), "total_models": len(models)}


def test_picker_defaults_to_subscription_and_labels_metered():
    rows = [_row("openai-api", [ASTRA, "gpt-5.5"]), _row("anthropic", ["claude"]),
            _row("openai-codex", [ASTRA])]
    out = _prefer_subscription_rows(rows)
    slugs = [r["slug"] for r in out]
    assert slugs.index("openai-codex") < slugs.index("openai-api")
    codex, metered = (next(r for r in out if r["slug"] == s) for s in ("openai-codex", "openai-api"))
    assert codex["billing"] == "subscription"
    assert metered["billing"] == "metered" and metered["subscription_overlap_models"] == [ASTRA]
    assert ASTRA in metered["models"]  # still explicitly selectable
    assert "billed per token" in metered["name"]


@pytest.mark.parametrize("current", ["openai-api", "anthropic", "auto"])
def test_unqualified_model_switch_prefers_subscription(current):
    from hermes_cli.models import detect_provider_for_model

    with patch("hermes_cli.models.cached_provider_model_ids", return_value=[ASTRA]), \
         patch("hermes_cli.models_detect.provider_has_credentials", return_value=True):
        assert detect_provider_for_model(ASTRA, current) == ("openai-codex", ASTRA)


def test_runtime_uses_pro_token_and_rejects_free_fallback(pool_home):
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_cli.auth import AuthError

    runtime = resolve_runtime_provider(requested="openai-codex", target_model=ASTRA)
    assert runtime["api_key"] == PRO
    # An explicit token (and the singleton fallback) must pass the same final guard.
    with pytest.raises(AuthError, match="entitled"):
        resolve_runtime_provider(requested="openai-codex", target_model=ASTRA, explicit_api_key=FREE)


def test_model_options_rpc_prefers_subscription(monkeypatch):
    from tui_gateway import server
    from hermes_cli.inventory import load_picker_context

    rows = [_row("openai-api", [ASTRA]), _row("openai-codex", [ASTRA])]
    monkeypatch.setattr(server, "_model_picker_context", lambda agent: load_picker_context())
    monkeypatch.setattr("hermes_cli.model_switch.list_authenticated_providers", lambda **kw: rows)
    monkeypatch.setattr("hermes_cli.inventory._apply_pricing", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.inventory._apply_capabilities", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.inventory._apply_featured", lambda *a, **kw: None)
    response = server._methods["model.options"](1, {})
    offered = [r for r in response["result"]["providers"] if ASTRA in r["models"]]
    assert offered[0]["slug"] == "openai-codex"
    assert "billed per token" in next(r for r in offered if r["slug"] == "openai-api")["name"]


def test_session_create_prefers_subscription_but_honors_explicit_provider():
    from tui_gateway.methods_session_model_guard import create_overrides

    with patch("hermes_cli.models.cached_provider_model_ids", return_value=[ASTRA]), \
         patch("hermes_cli.models_detect.provider_has_credentials", return_value=True):
        assert create_overrides({"model": ASTRA})[0]["provider"] == "openai-codex"
        assert create_overrides({"model": ASTRA, "provider": "openai-api"})[0]["provider"] == "openai-api"
        assert create_overrides({"model": "gpt-5.5", "provider": "openai-api"})[0]["provider"] == "openai-api"
