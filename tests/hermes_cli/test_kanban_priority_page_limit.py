"""Bounded canonical capture must reject endlessly advancing snapshots."""
import pytest

from hermes_cli import kanban_priority_prepare as prepare


class EndlessSnapshot:
    def __init__(self):
        self.calls = 0

    def snapshot_page(self, board, table, **kwargs):
        self.calls += 1
        key = f'{self.calls:08d}'
        return {'table': 'tasks', 'head': 1, 'more': True, 'next': key,
                'rows': [{'id': key, 'status': 'ready', 'created_on': 'one', 'priority': 1}]}


def test_capture_rejects_page_limit_before_returning_partial_inventory():
    hub = EndlessSnapshot()
    with pytest.raises(ValueError, match='snapshot page limit exceeded'):
        prepare.capture(hub, board_id='board', exceptions=[])
    assert hub.calls == 10000
