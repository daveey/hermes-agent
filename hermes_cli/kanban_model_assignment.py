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
    from hermes_cli.config_effective import load_profile_config_effective, load_user_config_effective
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
        return load_profile_config_effective(home / 'config.yaml', fail_closed=True)


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


def validate_model_assignment(assignee, model, provider, *, check_registry=True, local_profile=True):
    """Validate pinned models only; unknown/custom providers and models remain supported.

    This is configuration validation, not an entitlement or credential probe. Never
    automatically select a paid provider to repair an incompatible subscription pin.
    Under ``model_routing.enforce_subscription_only`` the pin must also pass the registry
    check the worker runs at startup; otherwise the card is accepted and only crashes later.

    ``local_profile=False`` (a card whose ``runs_on`` rules this machine out) skips this machine's profile
    config: the landing machine's worker runs its own registry check. Only the machine-independent
    vendor/provider family check of an explicit provider remains. [daveey]
    """
    if not model:
        return
    model = model.strip()
    config = _assignee_config(assignee) if local_profile else None
    if check_registry:
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


def _family_mismatch(assignee, model, provider):
    """True when the pin fails the vendor/provider family check alone (registry check skipped)."""
    try:
        validate_model_assignment(assignee, model, provider, check_registry=False)
    except ModelProviderMismatchError:
        return True
    return False


def validate_task_pin(conn, task_id, model, provider):
    """Repinning follows create's execution-host policy, including explicit remote providers."""
    from hermes_cli.kanban_pin_provider import reject_spaced_pin, require_remote_pin_provider, resolve_pin_provider
    from hermes_cli.kanban_runs_on_capabilities import runs_elsewhere

    row = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    assignee = row['assignee'] if row else None
    # runs_on is a sync extension: unsynced boards need not have the column.
    runs_on = row['runs_on'] if row and 'runs_on' in row.keys() else None
    remote = runs_elsewhere(conn, runs_on)
    validate_model_assignment(assignee, model, provider, local_profile=not remote)
    if remote:
        require_remote_pin_provider(model, provider)
        return model, provider
    if not provider:
        # Same pin-time binding as create_task: resolve against the card's assignee, or refuse.
        model, provider = resolve_pin_provider(model, None, assignee=assignee)
        reject_spaced_pin(model, provider)
    return model, provider


def dispatch_guard(conn, task_id, *, lane, dry_run=False):
    """Check pins before quota/history guards can hide a repairable mismatch."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import check_respawn_guard

    return (route_model_mismatch(conn, kb.get_task(conn, task_id), dry_run=dry_run)
            or check_respawn_guard(conn, task_id, lane=lane))


REFUSED_PIN_COMMENT_AUTHOR = 'kanban-dispatcher'


def clear_refused_pin(conn, task_id, model, provider, allowed, *, author=REFUSED_PIN_COMMENT_AUTHOR):
    """[daveey] Drop a stored pin the routing registry refuses; the card runs on the profile default.

    A stored pin is history, not a request (the kanban sibling of resume's ``stale_managed_pin``).
    Compare-and-swap on the exact pin so a concurrent ``set-model`` wins. Never substitutes another
    concrete model or a paid provider: clearing is the only repair. Returns True when it cleared.
    """
    from hermes_cli import kanban_db as kb

    allowed = list(allowed or [])
    reason = ModelPinNotInRegistryError.code
    with kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET model_override = NULL, provider_override = NULL "
            "WHERE id = ? AND model_override IS ? AND provider_override IS ? AND status != 'archived' "
            "AND COALESCE(model_pin_mandatory, 0) = 0",  # a mandatory pin is never dropped for the default
            (task_id, model, provider))
        if cur.rowcount != 1:
            return False
        kb._append_event(conn, task_id, 'model_override_cleared',
                         {'model': model, 'provider': provider, 'reason': reason, 'allowed': allowed})
        on = f' on {provider}' if provider else ''
        kb.add_comment(conn, task_id, author,
                       f'Model pin {model}{on} was cleared: this host\'s model routing registry refuses it '
                       f'({reason}; registered: {", ".join(allowed) or "none"}), so its worker could not start. '
                       'The card now runs on the profile default. Pin a registered model with '
                       '`hermes kanban set-model` if it needs a specific one.')
    kb.notify_task_updated(conn, task_id, ('model_override', 'provider_override'))
    return True


def route_model_mismatch(conn, task, *, dry_run=False):
    """Return a typed guard reason when dispatch needs durable supervisor repair.

    [daveey] When the registry refuses the pin and no supervisor can take the assignment block,
    clear the pin and return None so this same tick dispatches the card on the profile default
    (previously it sat ``ready`` behind a cap-exempt hold indefinitely)."""
    from hermes_cli import kanban_db as kb

    try:
        from hermes_cli.kanban_model_neutral import routing_view
        route = routing_view(task)
        validate_model_assignment(task.assignee, route.model_override, route.provider_override)
    except ModelProviderMismatchError as exc:
        if not dry_run:
            from hermes_cli.kanban_assignment_routing import AssignmentRoutingError
            try:
                kb.block_task(conn, task.id, reason=str(exc), kind='assignment',
                              expected_run_id=task.current_run_id)
            except AssignmentRoutingError as routing_error:
                if (isinstance(exc, ModelPinNotInRegistryError)
                        and not _family_mismatch(task.assignee, task.model_override, task.provider_override)
                        and clear_refused_pin(conn, task.id, task.model_override, task.provider_override,
                                              exc.allowed)):
                    kb._log.warning('kanban: cleared refused model pin %s/%s on %s (no supervisor: %s)',
                                    task.model_override, task.provider_override, task.id, routing_error)
                    return None
                # One invalid card must not starve the rest of the board. The
                # dispatcher records this guard hold; leave recovery agent-owned.
                kb._log.warning('kanban: model assignment repair for %s: %s', task.id, routing_error)
        return exc.code
    return None
