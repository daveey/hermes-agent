"""Capture/verify refuse snapshot page heads that ever decrease.

``check_interval`` replays the feed from the first page head to the final
head, so no snapshot page may have been read at a head above the final head.
The hub head is a monotonic feed seq; a page head below an earlier page head
(a malformed server) refuses even when it stays above the first head and the
final head is above the last page head.
"""
import pytest

from hermes_cli import kanban_priority_prepare as prepare
from tests.hermes_cli.test_kanban_priority_feed import Busy

ROWS = [{'id': 'a', 'priority': 10000, 'created_on': 'one', 'status': 'ready'},
        {'id': 'b', 'priority': 50000, 'created_on': 'two', 'status': 'done'},
        {'id': 'c', 'priority': 20000, 'created_on': 'one', 'status': 'ready'}]


class ThreePages(Busy):
    """Three one-row pages at the given heads, then the final read at ``final``."""

    def __init__(self, heads, final=4):
        super().__init__()
        self.heads = heads
        self.final = final

    def snapshot_page(self, board, table, after='', limit=1000):
        if limit == 1:
            return {'table': 'tasks', 'head': self.final, 'rows': ROWS[:1], 'more': True, 'next': 'a'}
        index = [''] + [r['id'] for r in ROWS]
        i = index.index(after)
        return {'table': 'tasks', 'head': self.heads[i], 'rows': ROWS[i:i + 1],
                'more': i < 2, 'next': ROWS[i]['id']}


def test_monotonic_page_heads_stay_accepted():
    for heads in ([1, 2, 3], [1, 1, 1], [1, 3, 3], [1, 2, 4]):
        manifest = prepare.capture(ThreePages(heads), board_id='board', exceptions=['b'])
        assert [r['id'] for r in manifest['rows']] == ['a', 'c']
        assert prepare.verify(ThreePages(heads), manifest, undo=True)['verified'] == 2


@pytest.mark.parametrize('heads', [
    pytest.param([1, 3, 2], id='decrease-above-first'),
    pytest.param([1, 5, 2], id='middle-page-above-final'),
    pytest.param([2, 3, 2], id='back-to-first'),
])
def test_non_monotonic_page_heads_refuse_capture_and_verify(heads):
    with pytest.raises(ValueError, match='changed'):
        prepare.capture(ThreePages(heads), board_id='board', exceptions=['b'])
    manifest = prepare.capture(ThreePages([1, 2, 3]), board_id='board', exceptions=['b'])
    with pytest.raises(ValueError, match='changed'):
        prepare.verify(ThreePages(heads), manifest, undo=True)


def test_page_head_above_final_head_refuses():
    with pytest.raises(ValueError, match='changed'):
        prepare.capture(ThreePages([1, 2, 5]), board_id='board', exceptions=['b'])
