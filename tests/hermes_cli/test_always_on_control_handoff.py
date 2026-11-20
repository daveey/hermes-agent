"""Busy always-on predecessor must not pin Desktop model.options after a warm successor serves.

Incident 2026-11-20: roller switched active.json to :9148 while :9149 stayed busy
(unpausable turns). The TCP-splice proxy kept the client's existing sockets on
:9149, which 503'd model.options on code skew. Retirement correctly refused to
kill the turn. Control-plane reads must hand off to the successor instead.
"""

from __future__ import annotations

import asyncio
import json
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
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


def test_pop_backend_port_binds_this_backend_port(home):
    env = {ro.ENV_BACKEND: "9150"}
    assert ro.pop_backend_port(env) == 9150
    assert ro.ENV_BACKEND not in env
    assert ro.this_backend_port() == 9150


def test_model_options_control_path_encodes_lowercase_true_and_profile():
    assert ro.model_options_control_path() == "/api/model/options"
    assert ro.model_options_control_path(explicit_only=True) == "/api/model/options?explicit_only=true"
    path = ro.model_options_control_path(
        profile="ember", refresh=True, include_unconfigured=True, explicit_only=True)
    assert path.startswith("/api/model/options?")
    assert "profile=ember" in path
    assert "refresh=true" in path
    assert "include_unconfigured=true" in path
    assert "explicit_only=true" in path
    assert "True" not in path


def test_handoff_refuses_non_allowlisted_successor_port(home):
    ro.write_active(80)
    hits = []

    def get(port, path, timeout):
        hits.append(port)
        return 200, 0.0, {"ok": True}

    assert ro.handoff_control_read(9149, "/api/model/options", get=get) is None
    assert hits == []


def _serve(handler_cls):
    httpd = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


def test_loopback_control_get_sends_session_token_and_parses_dict(home, monkeypatch):
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"] = self.path
            seen["token"] = self.headers.get("X-Hermes-Session-Token")
            body = json.dumps({"providers": [{"slug": "xai"}], "model": "live"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    httpd, port = _serve(Handler)
    monkeypatch.setattr(ro.aod, "ensure_credentials", lambda home=None: ("tok-abc", "nonce"))
    try:
        status, elapsed, body = ro._loopback_control_get(port, "/api/model/options?explicit_only=true", 2.0)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 200
    assert elapsed >= 0
    assert body == {"providers": [{"slug": "xai"}], "model": "live"}
    assert seen == {"path": "/api/model/options?explicit_only=true", "token": "tok-abc"}


def test_loopback_control_get_http_error_none_and_non_dict_json(home, monkeypatch):
    monkeypatch.setattr(ro.aod, "ensure_credentials", lambda home=None: ("tok-abc", "nonce"))

    class ErrorHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_error(503)

        def log_message(self, format, *args):
            pass

    httpd, port = _serve(ErrorHandler)
    try:
        status, _elapsed, body = ro._loopback_control_get(port, "/api/model/options", 2.0)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 503 and body is None

    class ArrayHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'["not-a-dict"]')

        def log_message(self, format, *args):
            pass

    httpd, port = _serve(ArrayHandler)
    try:
        status, _elapsed, body = ro._loopback_control_get(port, "/api/model/options", 2.0)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 200 and body is None

    status, _elapsed, body = ro._loopback_control_get(1, "/api/model/options", 0.2)
    assert status is None and body is None


def test_stale_dashboard_builds_successor_query(home, monkeypatch):
    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("74e8765b42", "fcda64965f"))
    monkeypatch.setattr(ro, "this_backend_port", lambda: 9149)
    calls = []

    def fake(port, path, **_k):
        calls.append((port, path))
        return {"providers": [{"slug": "xai", "models": ["grok-4"]}]}

    monkeypatch.setattr(ro, "handoff_control_read", fake)
    result = asyncio.run(_rt_models.get_model_options(profile="ember", refresh=True, explicit_only=True))
    assert result["providers"]
    assert calls == [(9149, ro.model_options_control_path(
        profile="ember", refresh=True, explicit_only=True))]


def test_stale_dashboard_handoff_does_not_block_event_loop(home, monkeypatch):
    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("74e8765b42", "fcda64965f"))
    monkeypatch.setattr(ro, "this_backend_port", lambda: 9149)
    entered = threading.Event()
    release = threading.Event()

    def slow(*_a, **_k):
        entered.set()
        if not release.wait(timeout=2.0):
            raise TimeoutError("event loop did not progress while successor GET blocked")
        return {"providers": [{"slug": "xai", "models": ["grok-4"]}]}

    monkeypatch.setattr(ro, "handoff_control_read", slow)

    async def run():
        ticks = []

        async def ticker():
            deadline = asyncio.get_running_loop().time() + 2.0
            while not entered.is_set():
                if asyncio.get_running_loop().time() > deadline:
                    break
                ticks.append("pre")
                await asyncio.sleep(0)
            for _ in range(5):
                ticks.append("mid")
                await asyncio.sleep(0)
            release.set()

        result, _ = await asyncio.gather(_rt_models.get_model_options(), ticker())
        return result, ticks

    result, ticks = asyncio.run(run())
    assert result == {"providers": [{"slug": "xai", "models": ["grok-4"]}]}
    assert ticks.count("mid") >= 5


def test_stale_model_options_rpc_forwards_session_profile_and_live_model(home, monkeypatch):
    from tui_gateway import server

    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("74e8765b42", "fcda64965f"))
    monkeypatch.setattr(ro, "this_backend_port", lambda: 9149)
    calls = []

    def fake(_port, path, **_k):
        calls.append(path)
        return {"providers": [{"slug": "xai", "models": ["launch-model"]}],
                "model": "launch-model", "provider": "anthropic"}

    monkeypatch.setattr(ro, "handoff_control_read", fake)
    ember = home / "profiles" / "ember"
    ember.mkdir(parents=True)
    agent = types.SimpleNamespace(model="session-model", provider="anthropic")
    monkeypatch.setitem(server._sessions, "tile-1", {"agent": agent, "profile_home": str(ember)})
    resp = server._methods["model.options"]("rid", {"session_id": "tile-1", "explicit_only": True})
    assert calls == [ro.model_options_control_path(profile="ember", explicit_only=True)], calls
    assert resp.get("result", {}).get("model") == "session-model", resp
    assert resp.get("result", {}).get("provider") == "anthropic", resp
    assert resp["result"]["providers"] == [{"slug": "xai", "models": ["launch-model"]}]


def test_stale_model_options_rpc_refuses_unknown_session_scope(home, monkeypatch):
    from tui_gateway import server

    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("74e8765b42", "fcda64965f"))
    monkeypatch.setattr(ro, "this_backend_port", lambda: 9149)
    calls = []
    monkeypatch.setattr(ro, "handoff_control_read", lambda *a, **k: calls.append(a) or {"providers": []})
    resp = server._methods["model.options"]("rid", {"session_id": "missing-tile", "explicit_only": True})
    assert resp.get("error", {}).get("code") == 5098, resp
    assert calls == []


# --- mutation-closing tests (review 5991148685) ---------------------------------------------


def test_active_or_warming_backend_never_calls_the_getter(home):
    calls = []

    def get(*a, **_k):
        calls.append(a)  # recorded outside any swallowed AssertionError
        return 200, 0.0, {"ok": True}

    ro.write_active(9149, warming=[9148])
    assert ro.handoff_control_read(9149, "/api/model/options", get=get) is None
    ro.write_active(9149)
    assert ro.handoff_control_read(9149, "/api/model/options", get=get) is None
    assert calls == []


def _rpc(home, monkeypatch, params, sessions=None, payload=None):
    from tui_gateway import server

    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("74e8765b42", "fcda64965f"))
    monkeypatch.setattr(ro, "this_backend_port", lambda: 9149)
    calls = []
    body = payload if payload is not None else {"providers": [], "provider": "xai", "model": "m"}
    monkeypatch.setattr(ro, "handoff_control_read", lambda _p, path, **_k: calls.append(path) or body)
    for sid, sess in (sessions or {}).items():
        monkeypatch.setitem(server._sessions, sid, sess)
    return server._methods["model.options"]("rid", params), calls


def test_rpc_unnameable_profile_home_refuses(home, monkeypatch):
    odd = home / "elsewhere"
    odd.mkdir()
    resp, calls = _rpc(home, monkeypatch, {"session_id": "s1"}, {"s1": {"agent": None, "profile_home": str(odd)}})
    assert resp.get("error", {}).get("code") == 5098, resp
    assert calls == []


def test_rpc_explicit_profile_param_is_forwarded(home, monkeypatch):
    (home / ".hermes" / "profiles" / "ember").mkdir(parents=True)
    resp, calls = _rpc(home, monkeypatch, {"profile": "ember"})
    assert "result" in resp, resp
    assert calls == [ro.model_options_control_path(profile="ember")]
    assert "profile=ember" in calls[0]


def test_rpc_session_without_profile_home_uses_launch_profile(home, monkeypatch):
    resp, calls = _rpc(home, monkeypatch, {"session_id": "s2"}, {"s2": {"agent": None}})
    assert "result" in resp, resp
    assert calls == [ro.model_options_control_path()]


def test_rpc_default_profile_home_is_omitted_not_named(home, monkeypatch):
    default_home = home / ".hermes"
    default_home.mkdir(exist_ok=True)
    resp, calls = _rpc(home, monkeypatch, {"session_id": "s3"}, {"s3": {"agent": None, "profile_home": str(default_home)}})
    assert "result" in resp, resp
    assert calls == [ro.model_options_control_path()]
    assert "profile" not in calls[0]


def test_rpc_forwards_every_flag(home, monkeypatch):
    resp, calls = _rpc(home, monkeypatch, {"explicit_only": True, "refresh": True, "include_unconfigured": True})
    assert "result" in resp, resp
    assert calls == [ro.model_options_control_path(explicit_only=True, refresh=True, include_unconfigured=True)]
    for flag in ("explicit_only=true", "refresh=true", "include_unconfigured=true"):
        assert flag in calls[0]


def test_rest_forwards_include_unconfigured(home, monkeypatch):
    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("74e8765b42", "fcda64965f"))
    monkeypatch.setattr(ro, "this_backend_port", lambda: 9149)
    calls = []
    monkeypatch.setattr(ro, "handoff_control_read", lambda _p, path, **_k: calls.append(path) or {"providers": []})
    asyncio.run(_rt_models.get_model_options(include_unconfigured=True))
    assert calls == [ro.model_options_control_path(include_unconfigured=True)]
    assert "include_unconfigured=true" in calls[0]


@pytest.mark.parametrize("live,handoff_provider", [("anthropic", "xai"), ("custom", "custom:corp")])
def test_rpc_refuses_when_live_provider_differs_from_successor_rows(home, monkeypatch, live, handoff_provider):
    agent = types.SimpleNamespace(model="live-model", provider=live)
    payload = {"provider": handoff_provider, "model": "disk-model",
               "providers": [{"slug": handoff_provider, "is_current": True, "models": ["disk-model"]}]}
    resp, calls = _rpc(home, monkeypatch, {"session_id": "s4", "explicit_only": True},
                       {"s4": {"agent": agent}}, payload)
    assert calls, "successor must have been asked"
    assert resp.get("error", {}).get("code") == 5098, resp
    assert "result" not in resp


def test_rpc_matching_provider_overlays_live_model_only(home, monkeypatch):
    agent = types.SimpleNamespace(model="live-model", provider="xai")
    payload = {"provider": "xai", "model": "disk-model",
               "providers": [{"slug": "xai", "is_current": True, "models": ["disk-model"]}]}
    resp, _ = _rpc(home, monkeypatch, {"session_id": "s5"}, {"s5": {"agent": agent}}, payload)
    assert resp["result"]["model"] == "live-model"
    assert resp["result"]["provider"] == "xai"
    assert resp["result"]["providers"] == payload["providers"]


@pytest.mark.parametrize("port", [0, 80, 9200])
def test_non_always_on_predecessor_never_reads_active_catalog(home, port):
    ro.write_active(9148)
    calls = []

    def get(*args):
        calls.append(args)
        return 200, 0.0, {"providers": []}

    assert ro.handoff_control_read(port, "/api/model/options", get=get) is None
    assert calls == []


@pytest.mark.parametrize("agent", [types.SimpleNamespace(model="live-model", provider=""),
                                  types.SimpleNamespace(model="live-model")], ids=["empty", "missing"])
def test_rpc_empty_live_provider_preserves_successor_rows_and_overlays_model(home, monkeypatch, agent):
    payload = {"provider": "xai", "model": "disk-model",
               "providers": [{"slug": "xai", "is_current": True, "models": ["disk-model"]}]}
    resp, calls = _rpc(home, monkeypatch, {"session_id": "empty-provider"},
                       {"empty-provider": {"agent": agent}}, payload)
    assert calls == [ro.model_options_control_path()]
    assert resp["result"] == {**payload, "model": "live-model"}
    assert payload["model"] == "disk-model", "overlay must not mutate the successor payload"
