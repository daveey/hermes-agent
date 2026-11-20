"""Reset host-loop retirement without racing a background import."""

import importlib
import sys


def reset_retired_host_loops(module_name="hermes_cli.host_loop_lease"):
    if module_name in sys.modules:
        # sys.modules publishes before exec_module completes on a background thread.
        # import_module waits for that thread's import lock; a direct lookup does not.
        lease = importlib.import_module(module_name)
        with lease._RETIRE_MU:
            lease._RETIRED.clear()
