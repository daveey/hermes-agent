"""Restart probes observe the submitter's replacement, not a competing launcher."""
from unittest.mock import Mock

import pytest

from tests.gateway.fixtures import kanban_restart_continue_probe as probe


def test_replacement_waits_for_identified_ready_owner(monkeypatch, tmp_path):
    previous = {'pid': 1, 'instance_id': 'old'}
    ready = {'state': 'ready', 'pid': 2, 'instance_id': 'new'}
    control = Mock(side_effect=[OSError('socket absent'), {'state': 'starting'}, ready])
    monkeypatch.setattr(probe, 'control', control)
    assert probe.restarted_owner(tmp_path, previous) is ready
    assert control.call_args_list == [((tmp_path, 'identify'),)] * 3


@pytest.mark.parametrize('pid,instance', [(1, 'new'), (2, 'old')])
def test_replacement_rejects_previous_owner_identity(monkeypatch, tmp_path, pid, instance):
    monkeypatch.setattr(probe, 'control', lambda *args: {'state': 'ready', 'pid': pid, 'instance_id': instance})
    with pytest.raises(AssertionError):
        probe.restarted_owner(tmp_path, {'pid': 1, 'instance_id': 'old'})
