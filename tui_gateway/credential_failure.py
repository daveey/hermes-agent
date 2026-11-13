"""Client-safe credential diagnostics. Call inside the owning profile's runtime scope.

Read pool state only: never select/refresh credentials or clear a cooldown while diagnosing.
"""
import time


def credential_failure_fields(provider: str, *, pool=None) -> dict:
    from agent.credential_pool import PooledCredential, STATUS_DEAD, STATUS_EXHAUSTED, _exhausted_until
    from hermes_cli.auth import read_credential_pool

    fields = {"failure_kind": "auth_missing", "provider": str(provider or ""), "reset_at": None}
    entries = (list(pool.entries()) if pool is not None else
               [PooledCredential.from_dict(provider, row) for row in read_credential_pool(provider)
                if isinstance(row, dict)])
    live = [entry for entry in entries if entry.last_status != STATUS_DEAD]
    if not live:
        return fields
    resets = []
    now = time.time()
    for entry in live:
        if entry.last_status != STATUS_EXHAUSTED or not (
            entry.last_error_code in {402, 429}
            or entry.failure_reason in {"rate_limit", "billing", "quota"}
        ):
            return fields
        reset = _exhausted_until(entry, sole_credential=len(live) == 1)
        if reset is not None and reset <= now:
            return fields
        if reset is not None:
            resets.append(reset)
    return {**fields, "failure_kind": "exhausted", "reset_at": min(resets) if resets else None}


def client_failure_fields(failure) -> dict:
    """[daveey] ``agent_failure`` as a client may see it. ``failure_resets_at`` is the automation replay's own
    clock (``automation_replay.replay_failed_wake``): the wire contracts declare ``reset_at`` instead, so it
    fills ``reset_at`` when that is unset and never travels under its own name."""
    if not isinstance(failure, dict):
        return failure
    fields = {k: v for k, v in failure.items() if k != "failure_resets_at"}
    if fields.get("reset_at") is None and failure.get("failure_resets_at") is not None:
        fields["reset_at"] = failure["failure_resets_at"]
    return fields


def exception_failure_fields(error, provider: str = "", *, pool=None) -> dict:
    """Only credential failures get setup metadata; unrelated session failures stay unrelated."""
    from agent.auxiliary_unavailable import ProviderCredentialsExhaustedError, ProviderNotConfiguredError
    from hermes_cli.auth import AuthError
    from hermes_cli.fallback_errors import ProviderFallbackError

    if isinstance(error, ProviderFallbackError):
        # Keep the primary's identity; resolving a fallback successfully never reaches this path.
        if error.failures:
            primary, _, cause = error.failures[0]
            return exception_failure_fields(cause, primary or provider, pool=pool)
    if not isinstance(error, (AuthError, ProviderCredentialsExhaustedError, ProviderNotConfiguredError)):
        return {}
    fields = credential_failure_fields(provider or getattr(error, "provider", "") or "", pool=pool)
    from hermes_cli.auth_constants import CODEX_RATE_LIMITED_CODE
    if isinstance(error, ProviderCredentialsExhaustedError) or (
        isinstance(error, AuthError) and error.code == CODEX_RATE_LIMITED_CODE and not error.relogin_required
    ):
        fields["failure_kind"] = "exhausted"
    return fields


def build_failure_fields(error, session: dict) -> dict:
    from tui_gateway import server
    overrides = session.get("model_override") or session.get("resume_runtime_overrides") or {}
    provider = overrides.get("provider") or getattr(error, "provider", "") or ""
    if not provider:
        _, provider = server._resolve_startup_runtime()
        cfg_model = server._load_cfg().get("model")
        provider = provider or (cfg_model.get("provider") if isinstance(cfg_model, dict) else "")
    return exception_failure_fields(error, str(provider or ""))


def provider_not_configured(error) -> bool:
    from agent.auxiliary_unavailable import ProviderNotConfiguredError
    from hermes_cli.fallback_errors import ProviderFallbackError
    if isinstance(error, ProviderFallbackError):
        return bool(error.failures) and all(provider_not_configured(exc) for _, _, exc in error.failures)
    return isinstance(error, ProviderNotConfiguredError)


def session_failure_payload(event: str, payload: dict, session: dict) -> dict:
    """Cover all session error producers through the common emit boundary, without text sniffing."""
    if event not in {"error", "message.complete"} or "failure_kind" in payload:
        return payload
    surface = payload.get("error_surface") or {}
    code = surface.get("code")
    if code == "agent_init_failed" and session.get("agent_failure"):
        return {**payload, **client_failure_fields(session["agent_failure"])}
    if surface.get("layer") != "auth" and code not in {"rate_limit", "billing", "quota_exceeded"}:
        return payload
    from tui_gateway.server import _session_profile_runtime_scope
    agent = session.get("agent")
    provider = str(surface.get("provider") or getattr(agent, "provider", "") or "")
    with _session_profile_runtime_scope(session):
        fields = credential_failure_fields(provider, pool=getattr(agent, "_credential_pool", None))
    return {**payload, **fields}
