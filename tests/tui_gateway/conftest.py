"""tui_gateway test fixtures.

Several files here import ``tui_gateway.server`` inside a ``patch.dict("sys.modules", {"hermes_constants":
MagicMock(...)})`` window so the module binds a fixed home. The server's import graph reaches
``agent.process_bootstrap`` → ``hermes_bootstrap``, which is process boot: PM dependency activation reads
the real install root through ``hermes_constants`` and exits the process when that is a MagicMock.
Importing it once here, before any window opens, keeps boot out of the mocked import.
"""

import hermes_bootstrap
import pytest


@pytest.fixture(autouse=True)
def _no_refused_turn_replay(monkeypatch):
    """A real 45 s timer would outlive its test and replay into the next one; ``test_refused_turn_retry`` turns it on."""
    from tui_gateway import server
    monkeypatch.setattr(server, "_REFUSED_RETRY_DELAYS_S", ())
