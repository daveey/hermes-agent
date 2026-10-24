"""Class policy adapter for CLI startup, lazy credentials, and session restore.

Keep the selector on the shell, concrete route on the agent. Reconstructing an
agent from a bare concrete model would lose the subscription-only failure fence.
"""
import logging
import sys

from agent.model_routing import is_class_selector


def resolve_cli_class(cli):
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_cli.config_effective import load_user_config_effective

    config = load_user_config_effective()
    requirements = {"tools": True, "streaming": True}
    context = (config.get("model") or {}).get("context_length")
    if context:
        requirements["min_context_tokens"] = context
    return resolve_runtime_provider(
        requested=cli.requested_provider, target_model=cli.model,
        explicit_api_key=cli._explicit_api_key, explicit_base_url=cli._explicit_base_url,
        routing_session_id=cli.session_id, routing_requirements=requirements,
        routing_config=config)


def probe_class_credentials(cli):
    try:
        resolve_cli_class(cli)
        return True, None
    except Exception as exc:
        logging.getLogger(__name__).debug("Class readiness probe failed", exc_info=True)
        return False, exc


def ensure_class_credentials(cli):
    from hermes_cli.runtime_provider import format_runtime_provider_error
    cli._credentials_rate_limited = False
    cli._credentials_terminal = False
    try:
        runtime = resolve_cli_class(cli)
    except Exception as exc:
        # No legacy fallback, even for unexpected auth/transport failures.
        logging.getLogger(__name__).debug("Class credential resolution failed", exc_info=True)
        print(format_runtime_provider_error(exc), file=sys.stderr)
        return False
    cli.api_key, cli.base_url = runtime["api_key"], runtime["base_url"]
    cli.provider, cli.api_mode = runtime["provider"], runtime["api_mode"]
    cli._credential_pool = runtime["credential_pool"]
    cli._provider_source = runtime.get("source")
    cli.acp_command, cli.acp_args = None, []
    cli.model_routing = runtime["model_routing"]
    return True


def agent_route(model, runtime):
    if not is_class_selector(model):
        return model, runtime
    # Credentials resolved above are for display/readiness only. The common
    # constructor reuses the same durable allocation and creates a fenced pool.
    return model, {"provider": "auto", "requested_provider": "auto"}


def skip_or_restore_class(cli, session_meta):
    return (not (session_meta or {}).get("model")
            or getattr(cli, "_explicit_model_override", False)
            or restore_class_route(cli, session_meta))


def restore_class_route(cli, session_meta):
    if getattr(cli, "_explicit_model_override", False):
        return False
    from agent.model_routing_restore import restore_route
    restored = restore_route(session_meta.get("model"), session_meta.get("model_config"))
    if restored is None:
        return False
    cli.model, cli.requested_provider = restored
    cli._explicit_api_key = cli._explicit_base_url = None
    return True
