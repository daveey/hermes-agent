"""Reject proven model/provider family mismatches without a stale model allowlist."""
from contextlib import ExitStack
from pathlib import Path


class ModelProviderMismatchError(ValueError):
    code = 'model_provider_mismatch'

    def __init__(self, model, provider):
        super().__init__(f'{self.code}: model {model!r} cannot run on provider {provider!r}; '
                         'set a compatible model/provider pair before dispatch')


class ModelPinNotInRegistryError(ModelProviderMismatchError):
    """[daveey] The assignee's managed subscription policy would refuse this pin at worker startup.

    Handled like a family mismatch (typed tool error, dispatch-time assignment block) under its own
    code, so the message names the registry's allowed models instead of a provider pairing."""
    code = 'model_pin_not_in_routing_registry'

    def __init__(self, model, provider, allowed):
        self.allowed = list(allowed)
        on = f' on provider {provider!r}' if provider else ''
        ValueError.__init__(
            self, f"{self.code}: model {model!r}{on} is not in this host's model routing registry "
            f"(managed subscription policy), so its worker would refuse to start. Pin one of: "
            f"{', '.join(self.allowed) or 'none'}; or leave model out (unpinned cards run on the profile default)")


def _assignee_config(assignee):
    """The config a worker for ``assignee`` reads; None for a non-profile lane (its executor decides)."""
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.profiles import resolve_profile_env
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    if not assignee:
        return load_user_config_effective()
    try:
        home = Path(resolve_profile_env(assignee))
    except (ValueError, FileNotFoundError):
        return None
    with ExitStack() as stack:
        stack.callback(reset_hermes_home_override, set_hermes_home_override(str(home)))
        stack.callback(reset_secret_scope, set_secret_scope(build_profile_secret_scope(home)))
        return load_user_config_effective(home / 'config.yaml', fail_closed=True)


def _check_managed_pin(model, provider, config):
    """Run the worker's own startup policy check (``pin_policy``) at pin time instead."""
    from agent.model_routing import is_class_selector
    from agent.model_routing_managed import ManagedPinRejectedError, managed_config, pin_policy

    if config is None or is_class_selector(model):
        return
    managed = managed_config(config)
    if managed is None:
        return
    try:
        pin_policy(model, provider, managed)
    except ManagedPinRejectedError as exc:
        raise ModelPinNotInRegistryError(model, provider, exc.allowed) from exc


def validate_model_assignment(assignee, model, provider):
    """Validate pinned models only; unknown/custom providers and models remain supported.

    This is configuration validation, not an entitlement or credential probe. Never
    automatically select a paid provider to repair an incompatible subscription pin.
    Under ``model_routing.enforce_subscription_only`` the pin must also pass the registry
    check the worker runs at startup; otherwise the card is accepted and only crashes later.
    """
    if not model:
        return
    model = model.strip()
    config = _assignee_config(assignee)
    _check_managed_pin(model, (provider or '').strip() or None, config)
    if not provider and assignee:
        if config is None:
            # Non-profile lanes are supported; their executor owns provider selection.
            return
        model_config = config.get('model', {})
        provider = model_config.get('provider') if isinstance(model_config, dict) else None
    provider = (provider or '').strip().lower()
    model = model.lower()
    # Native-only transports cannot serve these other vendors' model families.
    # Aggregators/custom endpoints intentionally have no family restriction.
    incompatible = {
        'openai-codex': ('claude-', 'anthropic/', 'gemini-', 'google/'),
        'anthropic': ('gpt-', 'openai/', 'gemini-', 'google/'),
    }
    if model.startswith(incompatible.get(provider, ())):
        raise ModelProviderMismatchError(model, provider)


def validate_task_pin(conn, task_id, model, provider):
    """``set_model_override``: validate against the card's assignee, then bind a provider-less pin."""
    from hermes_cli.kanban_db import _reject_spaced_pin
    from hermes_cli.kanban_pin_provider import resolve_pin_provider

    row = conn.execute('SELECT assignee FROM tasks WHERE id = ?', (task_id,)).fetchone()
    assignee = row[0] if row else None
    validate_model_assignment(assignee, model, provider)
    if not provider:
        # Same pin-time binding as create_task: resolve against the card's assignee, or refuse.
        model, provider = resolve_pin_provider(model, None, assignee=assignee)
        _reject_spaced_pin(model, provider)
    return model, provider


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
