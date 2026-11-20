"""Busy always-on predecessor must not pin Desktop model.options after a warm successor serves.

Incident 2026-11-20: roller switched active.json to :9148 while :9149 stayed busy
(unpausable turns). The TCP-splice proxy kept the client's existing sockets on
:9149, which 503'd model.options on code skew. Retirement correctly refused to
kill the turn. Control-plane reads must hand off to the successor instead.
"""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

import hermes_cli.always_on_rollover as ro
import hermes_cli.web_routers.models as _rt_models
from gateway import code_skew


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    if hasattr(ro, "_THIS_PORT"):
        monkeypatch.setattr(ro, "_THIS_PORT", None)
    return tmp_path


def _get_ok(port, path, timeout):
    return 200, 0.01, {"providers": [{"slug": "xai", "models": ["grok-4"]}]}


def test_superseded_backend_hands_model_options_to_the_active_successor(home):
    ro.write_active(9148)
    calls = []

    def get(port, path, timeout):
        calls.append((port, path, timeout))
        return _get_ok(port, path, timeout)

    body = ro.handoff_control_read(9149, "/api/model/options?explicit_only=true", get=get)
    assert body == {"providers": [{"slug": "xai", "models": ["grok-4"]}]}
    assert calls == [(9148, "/api/model/options?explicit_only=true", ro.HANDOFF_TIMEOUT_S)]


def test_active_or_warming_backend_does_not_handoff(home):
    ro.write_active(9149, warming=[9148])

    def get(*_a, **_k):
        raise AssertionError("successor must not be queried while this port still serves")

    assert ro.handoff_control_read(9149, "/api/model/options", get=get) is None
    ro.write_active(9149)
    assert ro.handoff_control_read(9149, "/api/model/options", get=get) is None


def test_handoff_does_not_forward_writes_or_prompts(home):
    ro.write_active(9148)
    hits = []

    def get(port, path, timeout):
        hits.append(path)
        return 200, 0.0, {"ok": True}

    assert ro.handoff_control_read(9149, "/api/model/set", get=get) is None
    assert ro.handoff_control_read(9149, "/api/prompt/submit", get=get) is None
    assert hits == []


def test_failed_or_non_json_successor_read_is_not_a_stale_bypass(home):
    ro.write_active(9148)
    assert ro.handoff_control_read(9149, "/api/model/options", get=lambda *_: (503, 0, {"detail": "no"})) is None
    assert ro.handoff_control_read(9149, "/api/model/options", get=lambda *_: (200, 0, "not-a-dict")) is None

    def boom(*_a, **_k):
        raise OSError("successor down")

    assert ro.handoff_control_read(9149, "/api/model/options", get=boom) is None


def test_stale_superseded_dashboard_returns_successor_payload_without_local_build(home, monkeypatch):
    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("74e8765b42", "fcda64965f"))
    monkeypatch.setattr(ro, "this_backend_port", lambda: 9149)
    ro.write_active(9148)
    monkeypatch.setattr(ro, "handoff_control_read", lambda *a, **k: {"providers": [{"slug": "xai", "models": ["grok-4"]}]})
    builds = []
    monkeypatch.setattr(
        "hermes_cli.inventory.build_model_options_payload",
        lambda *a, **k: builds.append(1) or {"providers": []},
    )
    result = asyncio.run(_rt_models.get_model_options())
    assert result == {"providers": [{"slug": "xai", "models": ["grok-4"]}]}
    assert builds == []


def test_stale_backend_without_successor_still_503s(home, monkeypatch):
    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("74e8765b42", "fcda64965f"))
    monkeypatch.setattr(ro, "this_backend_port", lambda: 9149)
    monkeypatch.setattr(ro, "handoff_control_read", lambda *a, **k: None)
    builds = []
    monkeypatch.setattr(
        "hermes_cli.inventory.build_model_options_payload",
        lambda *a, **k: builds.append(1) or {"providers": []},
    )
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_rt_models.get_model_options())
    assert excinfo.value.status_code == 503
    assert "restart" in str(excinfo.value.detail).lower()
    assert builds == []


def test_stale_model_options_rpc_hands_off_to_successor(home, monkeypatch):
    from tui_gateway import server

    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("74e8765b42", "fcda64965f"))
    monkeypatch.setattr(ro, "this_backend_port", lambda: 9149)
    payload = {"providers": [{"slug": "xai", "models": ["grok-4"]}]}
    monkeypatch.setattr(ro, "handoff_control_read", lambda *a, **k: payload)
    builds = []
    monkeypatch.setattr("hermes_cli.inventory.build_model_options_payload",
                        Mock(side_effect=lambda *a, **k: builds.append(1)))
    resp = server._methods["model.options"]("rid", {})
    assert resp.get("result") == payload, resp
    assert builds == []


def test_stale_model_save_key_still_refuses_when_a_successor_serves(home, monkeypatch):
    from tui_gateway import server

    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("74e8765b42", "fcda64965f"))
    monkeypatch.setattr(ro, "this_backend_port", lambda: 9149)
    monkeypatch.setattr(ro, "handoff_control_read", lambda *a, **k: {"providers": []})
    monkeypatch.setenv("HERMES_HOME", str(home))
    resp = server._methods["model.save_key"]("rid", {"slug": "zai", "api_key": "sk-canary"})
    assert resp.get("error", {}).get("code") == 5098, resp
    env = home / ".env"
    assert not env.exists() or "sk-canary" not in env.read_text(encoding="utf-8")
