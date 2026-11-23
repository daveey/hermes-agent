"""Serve-only roster/selector discovery, never imported by the reducers. [daveey]"""
import inspect

from hermes_cli import kanban_annotation_facts as annotations
from hermes_cli.fact_annotation_runtime import AnnotationRuntime
from hermes_cli.fact_annotation_views import selector_for


def seeds(runtime, readers=None):
    """Read canonical bootstrap windows without admitting legacy annotations.

    Bodies are discarded, and cache decorators are unwrapped. The existing
    source owner captures their selector membership; no body or source cache is
    retained here. Explicit startup callers/tests only; ``prepare`` never scans.
    """
    if readers is None:
        from hermes_cli.web_routers import profiles
        readers = (profiles.get_profiles_sessions, profiles.get_profiles_sessions_sidebar)
    selected = {name: {} for name in runtime.scopes}
    def collect(rows):
        for row in rows:
            name = row.get('profile', runtime.registry.fallback_profile)
            scope = runtime.scopes[name]
            identity = selector_for(scope, row)['row']
            # Identity includes ordered lineage, not just the visible tip ID.
            key = repr(identity)
            selected[name][key] = identity
    with annotations.collect_startup_seeds(collect):
        for reader in readers:
            reader = inspect.unwrap(reader)
            defaults = {}
            for name, parameter in inspect.signature(reader).parameters.items():
                value = parameter.default
                defaults[name] = getattr(value, 'default', value)
            reader(**defaults)
    return {name: list(rows.values()) for name, rows in selected.items()}


def prepare(*, on_created=None):
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli.process_identity import is_desktop_owned_backend
    from hermes_cli.profiles import get_active_profile_name
    from hermes_cli.web_routers.profiles import _profile_targets
    role = 'serve' if is_desktop_owned_backend() else 'dashboard'
    roster = dict(_profile_targets('native annotation startup'))
    runtime = AnnotationRuntime(roster, fallback_profile=get_active_profile_name(),
        gateway=activity._origin_scope()[0], stop_legacy=host.suspend_holder,
        start_legacy=lambda: host.resume_holder(role))
    if on_created is not None:
        on_created(runtime)
    try:
        # Inert install: no sessions scan, no source, no handle. Profiles
        # activate only when an actual consumer annotates.
        runtime.start({})
    except BaseException:
        runtime.close()
        raise
    return runtime
