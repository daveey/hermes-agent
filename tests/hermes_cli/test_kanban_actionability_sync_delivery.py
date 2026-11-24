"""The real sync round gates intent delivery on its successful second send. [daveey]"""
import pytest

from hermes_cli import kanban_actionability_intents as intents
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_sync_terminal_regress as base

kanban_home = base.kanban_home
svc = base.svc
live = base.live


@pytest.mark.parametrize('failure', [None, 'first', 'second'])
def test_delivery_after_second_success_only(live, kanban_home, svc, monkeypatch, failure):
    trace = []
    original = client._send
    def send(*args, **kwargs):
        phase = 'second' if kwargs['after_pull'] else 'first'
        trace.append(phase)
        if failure == phase:
            raise OSError('controlled send failure')
        return original(*args, **kwargs)
    def materialize(conn, hub, board):
        assert not conn.in_transaction
        assert board == base.BOARD
        trace.append('materialize')
    monkeypatch.setattr(client, '_send', send)
    monkeypatch.setattr(intents, 'materialize', materialize)
    if failure == 'second':
        with pytest.raises(OSError, match='controlled send failure'):
            base._round(kanban_home, svc)
        assert trace == ['first', 'second']
    else:
        base._round(kanban_home, svc)
        assert trace == ['first', 'second', 'materialize']
