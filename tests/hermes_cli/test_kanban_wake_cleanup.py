"""Wake probe cleanup delegates tree termination to the portable process helper."""
from unittest.mock import Mock

import pytest

from agent import deadline
from hermes_cli import kanban_wake_check as kwc


@pytest.mark.parametrize("terminated", [True, False])
def test_kill_probe_uses_portable_tree_cleanup(monkeypatch, terminated):
    proc = Mock(pid=999999999)
    kill_tree = Mock(return_value=terminated)
    monkeypatch.setattr(deadline, "kill_process_tree", kill_tree)

    kwc._kill(proc)

    kill_tree.assert_called_once_with(proc.pid)
    assert proc.kill.call_count == (0 if terminated else 1)


def test_kill_probe_tolerates_parent_already_gone(monkeypatch):
    proc = Mock(pid=999999999)
    proc.kill.side_effect = ProcessLookupError
    kill_tree = Mock(return_value=False)
    monkeypatch.setattr(deadline, "kill_process_tree", kill_tree)

    kwc._kill(proc)

    kill_tree.assert_called_once_with(proc.pid)
    proc.kill.assert_called_once_with()
