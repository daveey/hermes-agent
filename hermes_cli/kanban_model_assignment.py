"""Reject proven model/provider family mismatches without a stale model allowlist."""
from contextlib import ExitStack
from pathlib import Path


class ModelProviderMismatchError(ValueError):
    code = 'model_provider_mismatch'

    def __init__(self, model, provider):
        super().__init__(f'{self.code}: model {model!r} cannot run on provider {provider!r}; '
                         'set a compatible model/provider pair before dispatch')


def validate_model_assignment(assignee, model, provider):
    """Validate pinned models only; unknown/custom providers and models remain supported.

    This is configuration validation, not an entitlement or credential probe. Never
    automatically select a paid provider to repair an incompatible subscription pin.
    """
    if not model:
        return
    if not provider and assignee:
        from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
        from hermes_cli.config_effective import load_user_config_effective
        from hermes_cli.profiles import resolve_profile_env
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        try:
            home = Path(resolve_profile_env(assignee))
        except ValueError:
            # Non-profile lanes are supported; their executor owns provider selection.
            return
        with ExitStack() as stack:
            stack.callback(reset_hermes_home_override, set_hermes_home_override(str(home)))
            stack.callback(reset_secret_scope, set_secret_scope(build_profile_secret_scope(home)))
            config = load_user_config_effective(home / 'config.yaml', fail_closed=True)
        model_config = config.get('model', {})
        provider = model_config.get('provider') if isinstance(model_config, dict) else None
    provider = (provider or '').strip().lower()
    model = model.strip().lower()
    # Native-only transports cannot serve these other vendors' model families.
    # Aggregators/custom endpoints intentionally have no family restriction.
    incompatible = {
        'openai-codex': ('claude-', 'anthropic/', 'gemini-', 'google/'),
        'anthropic': ('gpt-', 'openai/', 'gemini-', 'google/'),
    }
    if model.startswith(incompatible.get(provider, ())):
        raise ModelProviderMismatchError(model, provider)


def dispatch_guard(conn, task_id, *, lane, dry_run=False):
    """Check pins before quota/history guards can hide a repairable mismatch."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import check_respawn_guard

    return (route_model_mismatch(conn, kb.get_task(conn, task_id), dry_run=dry_run)
            or check_respawn_guard(conn, task_id, lane=lane))


def route_model_mismatch(conn, task, *, dry_run=False):
    """Return a typed guard reason when dispatch needs durable supervisor repair."""
    from hermes_cli import kanban_db as kb

    try:
        validate_model_assignment(task.assignee, task.model_override, task.provider_override)
    except ModelProviderMismatchError as exc:
        if not dry_run:
            from hermes_cli.kanban_assignment_routing import AssignmentRoutingError
            try:
                kb.block_task(conn, task.id, reason=str(exc), kind='assignment',
                              expected_run_id=task.current_run_id)
            except AssignmentRoutingError as routing_error:
                # One invalid card must not starve the rest of the board. The
                # dispatcher records this guard hold; leave recovery agent-owned.
                kb._log.warning('kanban: model assignment repair for %s: %s', task.id, routing_error)
        return exc.code
    return None
