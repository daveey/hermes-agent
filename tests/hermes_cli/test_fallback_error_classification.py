"""An exhausted route chain only inherits classifications shared by every route."""
import threading

import pytest

from hermes_cli.auth import is_rate_limited_auth_error
from hermes_cli.auth_constants import AuthError, CODEX_RATE_LIMITED_CODE
from hermes_cli.fallback_errors import ProviderFallbackError


def aggregate(*errors):
    return ProviderFallbackError([(f"provider-{i}", f"model-{i}", error)
                                  for i, error in enumerate(errors)])


def quota(wait=60):
    return AuthError("quota exhausted", code=CODEX_RATE_LIMITED_CODE, retry_after=wait)


def test_all_quota_retains_classification_and_earliest_window():
    error = aggregate(quota(120), quota(30), quota(90))
    assert is_rate_limited_auth_error(error)
    assert error.retry_after == 30
    assert not error.relogin_required


@pytest.mark.parametrize("hint", [None, 0, -1, float("nan"), float("inf"),
                                 float("-inf"), "invalid", True])
def test_all_quota_invalid_hint_does_not_publish_a_window(hint):
    error = aggregate(quota(120), quota(hint))
    assert is_rate_limited_auth_error(error)
    assert error.retry_after is None


def test_all_relogin_retains_terminal_auth_classification():
    error = aggregate(AuthError("expired", relogin_required=True),
                      AuthError("revoked", relogin_required=True))
    assert isinstance(error, AuthError) and error.relogin_required
    assert not is_rate_limited_auth_error(error)
    assert error.retry_after is None


@pytest.mark.parametrize("other", [RuntimeError("network failed"),
    AuthError("expired", relogin_required=True), AuthError("unknown")])
@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_chain_does_not_inherit_quota_or_relogin(other, reverse):
    errors = [quota(), other]
    error = aggregate(*(errors[::-1] if reverse else errors))
    assert not is_rate_limited_auth_error(error)
    assert not error.relogin_required
    assert error.retry_after is None
    assert error.code == "fallback_exhausted"


def test_relogin_and_transient_is_not_terminal():
    error = aggregate(AuthError("expired", relogin_required=True), RuntimeError("network"))
    assert not error.relogin_required


def test_empty_chain_has_no_classification():
    error = aggregate()
    assert not is_rate_limited_auth_error(error)
    assert not error.relogin_required
    assert error.retry_after is None


def test_nested_quota_chain_and_original_failures_are_preserved():
    inner = aggregate(quota(120), quota(60))
    last = quota(90)
    error = aggregate(inner, last)
    assert is_rate_limited_auth_error(error)
    assert error.retry_after == 60
    assert error.failures == (("provider-0", "model-0", inner), ("provider-1", "model-1", last))
    assert "Primary provider-0/model-0" in str(error)
    assert "Fallback provider-1/model-1" in str(error)


@pytest.mark.parametrize("mixed", [False, True])
def test_tui_not_configured_envelope_only_for_homogeneous_chain(monkeypatch, mixed):
    from agent.auxiliary_unavailable import ProviderNotConfiguredError
    # Bootstrap recovery probes the live repository's git dir in a linked worktree.
    # This envelope test must never inspect or recover a real installation.
    monkeypatch.setattr("hermes_cli._early_recovery.restore_interrupted_pull", lambda *a: False)
    from tui_gateway import server

    errors = [ProviderNotConfiguredError("primary missing"),
              RuntimeError("network") if mixed else ProviderNotConfiguredError("fallback missing")]
    error = aggregate(*errors)
    events = []

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(server, "_make_agent", fail)
    monkeypatch.setattr(server, "_emit", lambda kind, sid, payload: events.append((kind, payload)))
    monkeypatch.setattr(server, "_deferred_build_agent_kwargs", lambda *a, **kw: {})
    monkeypatch.setattr(server, "_await_resume_history", lambda *a, **kw: True)
    for name in ("_set_session_context", "_clear_session_context", "_session_cwd",
                 "_bind_build_profile_scopes", "_finish_agent_build"):
        monkeypatch.setattr(server, name, lambda *a, **kw: None)
    session = {"session_key": "classification-key", "agent_ready": threading.Event()}
    monkeypatch.setitem(server._sessions, "classification-sid", session)
    server._start_agent_build("classification-sid", session)
    session["_agent_build_thread"].join(timeout=30)
    assert not session["_agent_build_thread"].is_alive()
    payload = next(payload for kind, payload in events if kind == "error")
    assert payload.get("code") == (None if mixed else "provider_not_configured")
    assert "primary missing" in payload["message"]
