"""User-safe replies for model resolution failures before a gateway turn starts."""


def resolution_error_result(exc: Exception, session_key: str | None) -> dict:
    """Log raw diagnostics while returning actionable, sanitized chat text."""
    # Read the existing runner bindings at call time to preserve patch seams.
    from gateway.run_turn_runner import logger, t

    # Model/credential resolution failed before the turn began; the raw text (URLs, status
    # codes) belongs in the log, and the chat gets the commands that fix it.
    logger.warning("Model resolution failed for session %s: %s", session_key or "", exc)
    from hermes_cli.auth import is_rate_limited_auth_error
    from hermes_cli.fallback_errors import ProviderFallbackError
    chain = exc if isinstance(exc, ProviderFallbackError) else exc.__cause__
    if isinstance(chain, ProviderFallbackError):
        from gateway.run import _gateway_provider_error_reply
        # Keep raw diagnostics in the log; describe each route using the
        # existing user-safe provider error mapping in messaging chats.
        reply = '\n'.join(
            f"{provider}/{model}: {_gateway_provider_error_reply(str(error))}"
            for provider, model, error in chain.failures
        )
        return {"final_response": reply, "messages": [], "api_calls": 0, "tools": []}
    if is_rate_limited_auth_error(exc.__cause__):
        # Quota cap with valid credentials: /login cannot help; name the reset window (#89401).
        from gateway.run import _gateway_provider_error_reply
        return {"final_response": _gateway_provider_error_reply(str(exc)),
                "messages": [], "api_calls": 0, "tools": []}
    return {
        "final_response": t("gateway.errors.no_credentials"),
        "messages": [], "api_calls": 0, "tools": [],
    }
