"""The actual model.options RPC offers the subscription before its billed duplicate."""
from tui_gateway import server
from hermes_cli.inventory import load_picker_context

ASTRA = "gpt-6-astra"


def _row(slug):
    return {"slug": slug, "name": slug, "models": [ASTRA], "total_models": 1}


def test_model_options_rpc_prefers_subscription(monkeypatch):
    rows = [_row("openai-api"), _row("openai-codex")]
    monkeypatch.setattr(server, "_model_picker_context", lambda agent: load_picker_context())
    monkeypatch.setattr("hermes_cli.model_switch.list_authenticated_providers", lambda **kw: rows)
    monkeypatch.setattr("hermes_cli.inventory._apply_pricing", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.inventory._apply_capabilities", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.inventory._apply_featured", lambda *a, **kw: None)
    response = server._methods["model.options"](1, {})
    offered = [r for r in response["result"]["providers"] if ASTRA in r["models"]]
    assert offered[0]["slug"] == "openai-codex"
    assert "billed per token" in next(r for r in offered if r["slug"] == "openai-api")["name"]


