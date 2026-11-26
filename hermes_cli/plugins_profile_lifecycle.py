"""Generation-bound plugin managers: delete retires hosts and cached module references."""
from __future__ import annotations

from pathlib import Path

from hermes_profile_resources import lease_for_path


def protect_profile_manager(manager, home: Path) -> None:
    with manager._discovery_lock:
        if getattr(manager, '_profile_resource_lease', None) is not None:
            manager._profile_resource_lease.assert_reopen_allowed()
            return
        lease = lease_for_path(home / 'profile.yaml')
        manager._profile_resource_lease = lease
        if lease is not None:
            lease.set_owner(manager, retire=retire_profile_manager)


def assert_manager_live(manager) -> None:
    lease = getattr(manager, '_profile_resource_lease', None)
    if lease is not None:
        lease.assert_reopen_allowed()


def retire_profile_manager(manager) -> None:
    from hermes_cli import plugins

    # The same lock excludes discovery and deferred materialization while its
    # on_unload callbacks stop pollers. A profile's host is dedicated to it only.
    with manager._discovery_lock:
        manager.unload()
        manager._evict_stale_persistent_registrations()
        host = getattr(manager, '_plugin_host_instance', None)
        if host is not None:
            host.shutdown()
        plugins._clear_plugin_submodules(manager)
    with plugins._plugin_managers_lock:
        for home, cached in list(plugins._plugin_managers_by_home.items()):
            if cached is manager:
                del plugins._plugin_managers_by_home[home]
        if plugins._plugin_manager is manager:
            plugins._plugin_manager = None
    manager._profile_resource_lease.release_owner()
