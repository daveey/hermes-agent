"""Process disappearance is successful verifier cleanup, not a polling error."""
from unittest.mock import Mock

import psutil
import pytest

from scripts.termux import validate_installed


@pytest.mark.parametrize("state", ["reaped", psutil.STATUS_ZOMBIE, "gone", "running"])
def test_cleanup_accepts_descendant_exit_during_status_probe(monkeypatch, state):
    from agent import deadline

    child = Mock(pid=123)
    child.poll.return_value = None
    descendant = Mock(pid=456)
    descendant.is_running.return_value = state != "gone"
    if state == "reaped":
        # The kernel can reap it after is_running(), before status().
        descendant.status.side_effect = psutil.NoSuchProcess(descendant.pid)
    else:
        descendant.status.return_value = state
    parent = Mock()
    parent.children.return_value = [descendant]
    monkeypatch.setattr(psutil, "Process", Mock(return_value=parent))
    kill = Mock()
    monkeypatch.setattr(deadline, "kill_process_tree", kill)

    if state == "running":
        monkeypatch.setattr(validate_installed.time, "monotonic", Mock(side_effect=[0, 0, 11]))
        monkeypatch.setattr(validate_installed.time, "sleep", Mock())
        with pytest.raises(RuntimeError, match=r"TUI children still alive: \[456\]"):
            validate_installed.stop_child_tree(child)
    else:
        validate_installed.stop_child_tree(child)

    kill.assert_called_once_with(child.pid)
    child.wait.assert_called_once_with(timeout=10)
    if state == "gone":
        descendant.status.assert_not_called()


def test_cleanup_does_not_hide_unobservable_descendant(monkeypatch):
    from agent import deadline

    child = Mock(pid=123)
    child.poll.return_value = None
    descendant = Mock(pid=456)
    descendant.is_running.return_value = True
    descendant.status.side_effect = psutil.AccessDenied(descendant.pid)
    parent = Mock()
    parent.children.return_value = [descendant]
    monkeypatch.setattr(psutil, "Process", Mock(return_value=parent))
    monkeypatch.setattr(deadline, "kill_process_tree", Mock())

    with pytest.raises(psutil.AccessDenied):
        validate_installed.stop_child_tree(child)
