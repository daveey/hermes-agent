"""A shared backend must retire only the deleted profile's host and manager."""
from pathlib import Path

import pytest

from hermes_constants import clear_named_profile_deleted, mark_named_profile_deleted, set_hermes_home_override, reset_hermes_home_override
from tests.hermes_state import test_profile_retirement_custody as helpers
from tests.waiting import wait_until

profile_home = helpers.profile_home


@pytest.mark.platforms('linux')
def test_profile_plugin_host_stops_and_same_name_gets_new_manager(profile_home):
    from hermes_cli import plugins
    from hermes_profile_resources import wait_profile_resources
    token = set_hermes_home_override(profile_home)
    host = None
    try:
        manager = plugins.get_plugin_manager()
        host = manager._plugin_host()
        host.ensure_started()
        process = host._proc
        assert process is not None and process.poll() is None
        mark_named_profile_deleted(profile_home)
        assert wait_profile_resources(profile_home, timeout=20)
        assert process.poll() is not None
        assert not host.alive
        with pytest.raises(FileNotFoundError):
            host.ensure_started()
        clear_named_profile_deleted(profile_home)
        # A new manager owns this profile even if its old directory is retained.
        replacement = plugins.get_plugin_manager()
        assert replacement is not manager
        assert Path(replacement.scope_key) == profile_home
        with pytest.raises(FileNotFoundError):
            manager.discover_and_load()
    finally:
        if host is not None:
            host.shutdown()
        plugins._reset_plugin_managers_for_tests()
        reset_hermes_home_override(token)
    wait_until(lambda: not any(lease.home == profile_home for lease in _leases()), timeout=10)


def _leases():
    from hermes_profile_resources import _lock, _leases
    with _lock:
        return list(_leases)
