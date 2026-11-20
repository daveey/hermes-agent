"""Session bookkeeping for explicit runtime switches."""
import logging
from copy import copy
from contextlib import nullcontext


class _DeferredSessionWrites:
    """Read the live compressor backend but publish mutations only after commit."""

    def __init__(self, backend, writes):
        self.backend, self.writes = backend, writes

    def __getattr__(self, name):
        method = getattr(self.backend, name)
        if not name.startswith(("set_", "clear_", "update_", "patch_")):
            return method

        def defer(*args, **kwargs):
            self.writes.append((method, args, kwargs))
            return True
        return defer


def _stage_compressor(compressor):
    """Delay the native compressor's durable resets until switch publication."""
    if compressor is None:
        return None, []
    # SessionDB and SDK clients contain locks and must remain shared handles.
    # Switch setters/resetters change scalar fields and mutable state containers,
    # not the bound durable backend itself.
    staged = copy(compressor)
    for key, value in vars(compressor).items():
        if isinstance(value, (dict, list, set)):
            setattr(staged, key, copy(value))
    writes = []
    backend = getattr(staged, '_session_db', None)
    if backend is not None:
        staged._session_db = _DeferredSessionWrites(backend, writes)
    return staged, writes


def _switch_changes(base, pristine, working, staged):
    """Only what the switch itself changed since its snapshot is published.

    Resolution can take seconds and the callers' ``running`` check is unlocked, so a turn may be
    admitted on the live agent meanwhile. Publishing the whole stale copy reverted that turn's state:
    its lease holder went back to None (the flush then skipped the lease check) or to an earlier
    turn's released holder (the flush was refused as a lost lease)."""
    changes = {}
    for key, value in staged.items():
        if key in working and value is working[key]:
            try:
                unchanged = value == pristine[key]
            except (TypeError, ValueError, RecursionError):
                unchanged = False  # incomparable content: publish, as before
        else:
            unchanged = key in base and value is base[key]
        if unchanged:
            continue
        changes[key] = value
    return changes


def switch_runtime(agent, model, provider, api_key, base_url, api_mode, capabilities):
    """Prepare managed switches privately, then publish after the allocation commits."""
    from agent.agent_runtime_helpers import _switch_model_runtime
    from agent.model_routing_managed import managed_config, resolve_managed_runtime
    from agent.model_routing_agent import persist_class_metadata

    config = managed_config()
    from agent.model_routing import is_class_selector
    if config is None and is_class_selector(model):
        from hermes_cli.config_effective import load_user_config_effective
        config = load_user_config_effective()
    temporary = isinstance(getattr(agent, '_one_turn_runtime', None), dict)
    if config is None and not temporary:
        return _switch_model_runtime(agent, model, provider, api_key, base_url, api_mode, capabilities)
    staged = copy(agent)
    base = dict(vars(agent))
    # Switch helpers mutate these containers in place. Never expose partial work
    # or clear live provenance while constructing a replacement.
    pristine = {key: copy(value) for key, value in base.items() if isinstance(value, (dict, list, set))}
    for key, value in pristine.items():
        setattr(staged, key, copy(value))
    working = {key: vars(staged)[key] for key in pristine}
    staged.context_compressor, compressor_writes = _stage_compressor(getattr(agent, 'context_compressor', None))
    staged._session_db = None

    def prepare(runtime):
        staged._credential_pool = runtime['credential_pool']
        _switch_model_runtime(staged, runtime['model'], runtime['provider'], runtime['api_key'],
                              runtime['base_url'], runtime['api_mode'], capabilities, managed=runtime)

    from agent.model_routing_runtime import resolve_class_runtime
    resolver = resolve_class_runtime if is_class_selector(model) else resolve_managed_runtime
    from agent.model_routing_runtime import temporary_resolution
    with temporary_resolution() if temporary else nullcontext():
        if config is None:
            _switch_model_runtime(staged, model, provider, api_key, base_url, api_mode, capabilities)
        else:
            resolver(model, provider, config=config, session_id=agent.session_id,
                     requirements=getattr(agent, '_model_routing_requirements', {}),
                     prepare_runtime=prepare, replace=True)
    staged._session_db = getattr(agent, '_session_db', None)
    backend = getattr(staged.context_compressor, '_session_db', None)
    if isinstance(backend, _DeferredSessionWrites):
        staged.context_compressor._session_db = backend.backend
    vars(agent).update(_switch_changes(base, pristine, working, vars(staged)))
    persist_class_metadata(agent)
    for method, args, kwargs in ([] if temporary else compressor_writes):
        method(*args, **kwargs)
    persist_switch_billing_route(agent)


def persist_switch_billing_route(agent) -> None:
    """Persist the billing route so dashboard Model cards show the post-switch provider."""
    if isinstance(getattr(agent, '_one_turn_runtime', None), dict):
        return
    session_db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if session_db is None or not session_id:
        return
    try:
        session_db.update_session_billing_route(
            session_id, provider=agent.provider, base_url=agent.base_url,
            billing_mode=getattr(agent, "api_mode", None),
        )
    except Exception:
        logging.getLogger(__name__).warning("Failed to persist billing route after model switch", exc_info=True)
