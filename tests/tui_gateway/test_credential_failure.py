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
    monkeypatch.setattr(server, "_load_cfg", dict)
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
    monkeypatch.setattr(server, "_load_cfg", dict)
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



def _strict_build(monkeypatch, exc):
    """Run the deferred build through the REAL ``_emit`` (contract-checked under test isolation)."""
    import threading
    from tui_gateway import server
    from tests.tui_gateway.test_provider_not_configured_code import _fail_build, _run_build
    monkeypatch.setattr(server, "_resolve_startup_runtime", lambda: ("m", "openai-codex"))
    monkeypatch.setattr(server, "_load_cfg", dict)
    frames, crashes = [], []
    monkeypatch.setattr(server, "write_json", lambda frame: frames.append(frame) or True)
    monkeypatch.setattr(threading, "excepthook", lambda args: crashes.append(args.exc_value))
    session = _fail_build(monkeypatch, exc)
    _run_build(session)
    return session, [f["params"]["payload"] for f in frames if f["params"]["type"] == "error"], crashes


def test_quota_walled_build_error_event_honours_its_contract(tmp_path, monkeypatch):
    """A typed quota failure knows its reset: the replay keeps it, the client reads it as ``reset_at``."""
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    from tui_gateway import server
    from tui_gateway.contracts import registry
    assert registry.STRICT  # the contract check raises here instead of only logging
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reset = 1793774268.0
    session, payloads, crashes = _strict_build(
        monkeypatch, SubscriptionQuotaUnavailable("openai-codex", "gpt-6-astra", reset))
    assert crashes == []
    assert len(payloads) == 1
    payload = payloads[0]
    assert "failure_resets_at" not in payload
    assert payload["reset_at"] == reset
    assert session["agent_failure"]["failure_resets_at"] == reset  # what the automation replay reads
    assert "failure_resets_at" not in (server._wait_agent(session, "r")["error"]["data"] or {})


def test_agent_init_failed_error_enrichment_honours_its_contract(tmp_path, monkeypatch):
    """``session_failure_payload`` folds ``agent_failure`` into later ``agent_init_failed`` events too."""
    from tui_gateway import server
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    frames = []
    monkeypatch.setattr(server, "write_json", lambda frame: frames.append(frame) or True)
    monkeypatch.setitem(server._sessions, "quota-test", {"agent_failure": {"failure_resets_at": 1793774268.0}})
    for event, payload in (("error", {"message": "opaque"}), ("message.complete", {"text": "opaque", "status": "error"})):
        server._emit(event, "quota-test", {**payload, "error_surface": {
            "layer": "runtime", "code": "agent_init_failed", "retryable": False}})
        result = frames[-1]["params"]["payload"]
        assert "failure_resets_at" not in result
        assert result["reset_at"] == 1793774268.0


def _prompt_turn_on_quota_build(monkeypatch, tmp_path, *, dead_build: bool):
    """Drive a submitted prompt (``_run_after_agent_ready``, prompt.submit's turn thread) onto a quota-walled
    build, recording the RPC error ``_wait_agent_for_prompt`` hands back and the client frames the turn emits."""
    import threading
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    from tui_gateway import server
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reset = 1793774268.0
    session, _payloads, crashes = _strict_build(
        monkeypatch, SubscriptionQuotaUnavailable("openai-codex", "gpt-6-astra", reset))
    assert crashes == [] and session["agent_failure"]["failure_resets_at"] == reset
    session.update({"history_lock": threading.Lock(), "running": True, "agent": None, "history": [],
                    "history_version": 0, "cols": 80, "inflight_turn": None})
    if dead_build:  # the build thread is gone but never set ready: the dead-build branch
        session["agent_ready"] = threading.Event()
        assert not session["_agent_build_thread"].is_alive()
    else:  # the build finished and set ready with its error: the final branch
        assert session["agent_ready"].is_set()
    monkeypatch.setattr(server, "_AGENT_BUILD_WAIT_SLICE", 0.01)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    real_wait, waited = server._wait_agent_for_prompt, []
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *a: waited.append(real_wait(*a)) or waited[-1])
    frames = []
    monkeypatch.setattr(server, "write_json", lambda frame: frames.append(frame) or True)
    server._run_after_agent_ready("rid", "sid", session, "hello", None, None, None)
    (err,) = waited
    complete = [f["params"]["payload"] for f in frames if f["params"]["type"] == "message.complete"]
    return session, reset, err, complete


@pytest.mark.parametrize("dead_build", [True, False], ids=["dead-build", "final-branch"])
def test_prompt_wait_rpc_error_reads_reset_at_not_the_replay_clock(tmp_path, monkeypatch, dead_build):
    """Both ``_wait_agent_for_prompt`` failure exits carry ``agent_failure`` as a client may see it."""
    session, reset, err, complete = _prompt_turn_on_quota_build(monkeypatch, tmp_path, dead_build=dead_build)
    data = err["error"]["data"]
    assert "failure_resets_at" not in data
    assert data["reset_at"] == reset
    assert session["agent_failure"]["failure_resets_at"] == reset  # the automation replay still reads it
    (payload,) = complete
    assert payload["status"] == "error"
    assert "failure_resets_at" not in payload
    assert payload["reset_at"] == reset


def test_client_failure_fields_keeps_a_reset_at_already_set(tmp_path, monkeypatch):
    """``failure_resets_at`` only fills an unset ``reset_at``: a known pool reset is never overwritten."""
    from tui_gateway import server
    from tui_gateway.credential_failure import client_failure_fields
    pool_reset, replay_reset = 4102444800, 1793774268.0
    failure = {"failure_kind": "exhausted", "reset_at": pool_reset, "failure_resets_at": replay_reset}
    assert client_failure_fields(failure) == {"failure_kind": "exhausted", "reset_at": pool_reset}
    assert failure["failure_resets_at"] == replay_reset  # the stored record is not mutated
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    frames = []
    monkeypatch.setattr(server, "write_json", lambda frame: frames.append(frame) or True)
    monkeypatch.setitem(server._sessions, "both-resets", {"agent_failure": failure})
    server._emit("error", "both-resets", {"message": "opaque", "error_surface": {
        "layer": "runtime", "code": "agent_init_failed", "retryable": False}})
    result = frames[-1]["params"]["payload"]
    assert "failure_resets_at" not in result
    assert result["reset_at"] == pool_reset
