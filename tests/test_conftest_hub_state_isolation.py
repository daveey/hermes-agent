"""The hermes-hub replica stays out of reach after per-test env isolation is undone. [daveey]

The autouse isolation fixture points ``HERMES_HUB_STATE`` at an empty per-test dir, but a background
thread a test leaves running reads the environment after that monkeypatch is undone. Before the
process-wide default, such a thread on hermes-hub loaded the host's live ``hub.db`` (~40 MB of task
rows per read) and pushed tests/tui_gateway/test_tui_gateway_server.py past the 512 MB per-file budget.
"""

if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard
import os
import threading
from pathlib import Path

from hermes_cli import kanban_session_activity


def test_hub_state_outside_the_test_scope_is_never_the_real_replica(monkeypatch):
    seen = {}

    def _late_reader():  # what a leaked background thread sees once the test's monkeypatch is gone
        seen["env"] = os.environ.get("HERMES_HUB_STATE")
        seen["path"] = kanban_session_activity._hub_db_path()

    monkeypatch.undo()  # back to the process-wide environment conftest set at import
    reader = threading.Thread(target=_late_reader)
    reader.start()
    reader.join(timeout=10)

    real_default = Path.home() / ".local" / "state" / "hermes-hub"
    assert seen["env"], "conftest must set a process-wide HERMES_HUB_STATE"
    assert Path(seen["env"]) != real_default
    assert not Path(seen["env"]).exists()
    assert seen["path"] is None
