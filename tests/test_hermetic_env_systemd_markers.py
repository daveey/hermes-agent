"""The hermetic test env drops systemd unit markers inherited from the host.

pytest launched from a Kanban worker / gateway terminal on a systemd host
inherits INVOCATION_ID (and NOTIFY_SOCKET etc.). Unscrubbed, the Kanban spawn
path took the restart-safe-scope branch and raised "cannot create restart-safe
systemd scope ... no current run id" on hosts only, never on CI.
"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import os

from tests._fixtures.env_filter import _HERMES_BEHAVIORAL_VARS

_SYSTEMD_MARKERS = (
    "INVOCATION_ID", "NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID", "JOURNAL_STREAM", "MANAGERPID",
)


def test_systemd_markers_are_scrubbed_per_test():
    for name in _SYSTEMD_MARKERS:
        assert name in _HERMES_BEHAVIORAL_VARS
        assert name not in os.environ, f"{name} leaked from the host into a test"


def test_worker_spawn_dispatch_is_in_process_without_opt_in():
    from tools.process_registry import restart_safe_gateway_child_argv

    dispatch = restart_safe_gateway_child_argv(
        ["true"], unit_suffix="kanban-t-run-missing", require_restart_safe_scope=True, outlives_parent=True,
    )
    assert dispatch.mode == "in_process"
