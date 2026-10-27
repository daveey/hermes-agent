"""Credential exhaustion is structured state, never a phrase in an exception."""
import json
import time
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def isolate_recovery(monkeypatch):
    # This worktree's gitdir belongs to the live installation; imports must not probe it.
    monkeypatch.setattr("hermes_cli._early_recovery.restore_interrupted_pull", lambda *a, **kw: False)


def pool(home, rows):
    (home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"anthropic": rows}}))


def row(reset=None, **kw):
    return {"id": "a", "provider": "anthropic", "auth_type": "oauth", "source": "manual",
            "access_token": "test-token", "last_status": "exhausted", "last_error_code": 429,
            "last_status_at": time.time(), "last_error_reset_at": reset, **kw}


@pytest.mark.parametrize("rows, expected", [([], "auth_missing"),
    ([row(4102444800), row(4102444900, id="b")], "exhausted"),
    ([row(4102444800), row(None, id="b", last_status="ready")], "auth_missing"),
    ([row(4102444800, last_status="dead")], "auth_missing"),
    ([row(4102444800, last_error_code=401)], "auth_missing"),
    ([row(1)], "auth_missing")])
def test_runtime_failure_reads_pool_not_error_words(tmp_path, monkeypatch, rows, expected):
    from tui_gateway import server
    from hermes_cli.auth import AuthError
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pool(tmp_path, rows)
    monkeypatch.setattr(server, "_resolve_startup_runtime", lambda: ("m", "anthropic"))
    monkeypatch.setattr(server, "_load_cfg", lambda: {"model": {"provider": "anthropic"}})
    def fail(*a, **kw):
        raise AuthError("opaque error without quota wording")
    monkeypatch.setattr(server, "_resolve_agent_model_runtime", fail)
    result = server.handle_request({"id": "r", "method": "setup.runtime_check", "params": {}})["result"]
    assert result["ok"] is False
    assert result["failure_kind"] == expected
    assert result["provider"] == "anthropic"
    assert result["reset_at"] == (4102444800 if expected == "exhausted" else None)


def test_classification_uses_live_pool_and_cooldown():
    from tui_gateway.credential_failure import credential_failure_fields
    from agent.credential_pool import PooledCredential
    entry = PooledCredential.from_dict("anthropic", row())
    fields = credential_failure_fields("anthropic", pool=SimpleNamespace(entries=lambda: [entry]))
    assert fields["failure_kind"] == "exhausted"
    assert fields["reset_at"] > time.time()


def test_failed_fallback_preserves_primary_exhaustion(tmp_path, monkeypatch):
    from tui_gateway.credential_failure import exception_failure_fields
    from hermes_cli.auth import AuthError
    from hermes_cli.fallback_errors import ProviderFallbackError
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pool(tmp_path, [row(4102444800)])
    error = ProviderFallbackError([
        ("anthropic", "m", AuthError("opaque")),
        ("openrouter", "other", AuthError("no credential")),
    ])
    assert exception_failure_fields(error) == {
        "failure_kind": "exhausted", "provider": "anthropic", "reset_at": 4102444800}
    assert exception_failure_fields(RuntimeError("quota exhausted rate limited"), "anthropic") == {}


def test_exhaustion_with_unknown_reset(tmp_path, monkeypatch):
    from tui_gateway.credential_failure import credential_failure_fields
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pool(tmp_path, [row(None, last_status_at=None)])
    assert credential_failure_fields("anthropic") == {
        "failure_kind": "exhausted", "provider": "anthropic", "reset_at": None}


def test_runtime_fallback_success_is_not_exhausted(tmp_path, monkeypatch):
    from tui_gateway import server
    pool(tmp_path, [row(4102444800)])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(server, "_resolve_startup_runtime", lambda: ("m", "anthropic"))
    monkeypatch.setattr(server, "_resolve_agent_model_runtime", lambda *a: ("other", {
        "provider": "custom", "api_key": "no-key-required", "source": "config"}))
    result = server.handle_request({"id": "r", "method": "setup.runtime_check", "params": {}})["result"]
    assert result["ok"] is True, result
    assert "failure_kind" not in result


def test_credential_warning_carries_same_fields(tmp_path, monkeypatch):
    from tui_gateway import server
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pool(tmp_path, [row(4102444800)])
    agent = SimpleNamespace(provider="anthropic", api_key="", model="m")
    result = server._session_info(agent, {"agent": agent})
    assert result["credential_warning"]
    assert result["failure_kind"] == "exhausted"
    assert result["provider"] == "anthropic"
    assert result["reset_at"] == 4102444800


def test_named_profile_failure_is_classified_before_scope_exits(tmp_path, monkeypatch):
    from tui_gateway import server
    from hermes_cli.auth import AuthError
    launch, named = tmp_path / "launch", tmp_path / "named"
    launch.mkdir()
    named.mkdir()
    pool(launch, [])
    pool(named, [row(4102444800)])
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "named")
    monkeypatch.setattr(server, "_profile_home", lambda name: named)
    monkeypatch.setattr(server, "_resolve_startup_runtime", lambda: ("m", "anthropic"))
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    def fail(*a, **kw):
        raise AuthError("opaque")
    monkeypatch.setattr(server, "_resolve_agent_model_runtime", fail)
    result = server.handle_request({"id": "r", "method": "setup.runtime_check",
                                    "params": {"profile": "named"}})["result"]
    assert result["failure_kind"] == "exhausted", result
    assert result["reset_at"] == 4102444800
    assert result["profile"] == "named"
    from tui_gateway.credential_failure import credential_failure_fields
    assert credential_failure_fields("anthropic")["failure_kind"] == "auth_missing"


def test_deferred_build_error_and_rpc_carry_exhaustion(tmp_path, monkeypatch):
    from tui_gateway import server
    from agent.auxiliary_unavailable import ProviderNotConfiguredError
    from tests.tui_gateway.test_provider_not_configured_code import _fail_build, _run_build
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pool(tmp_path, [row(4102444800)])
    monkeypatch.setattr(server, "_resolve_startup_runtime", lambda: ("m", "anthropic"))
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    events = []
    monkeypatch.setattr(server, "_emit", lambda kind, sid, payload: events.append((kind, payload)))
    session = _fail_build(monkeypatch, ProviderNotConfiguredError("opaque"))
    _run_build(session)
    payload = next(p for kind, p in events if kind == "error")
    assert payload["failure_kind"] == "exhausted"
    assert payload["provider"] == "anthropic"
    assert payload["reset_at"] == 4102444800
    assert "code" not in payload
    assert server._wait_agent(session, "r")["error"]["data"] == session["agent_failure"]


@pytest.mark.parametrize("event", ["error", "message.complete"])
def test_session_events_share_metadata(tmp_path, monkeypatch, event):
    from tui_gateway import server
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pool(tmp_path, [row(4102444800)])
    frames = []
    monkeypatch.setattr(server, "write_json", lambda frame: frames.append(frame))
    monkeypatch.setitem(server._sessions, "exhausted-test", {"agent": SimpleNamespace(provider="anthropic")})
    payload = {"error_surface": {"layer": "provider", "code": "rate_limit", "retryable": True}}
    payload.update({"message": "opaque"} if event == "error" else {"text": "opaque", "status": "error"})
    server._emit(event, "exhausted-test", payload)
    result = frames[-1]["params"]["payload"]
    assert result["failure_kind"] == "exhausted"
    assert result["provider"] == "anthropic"
    assert result["reset_at"] == 4102444800

