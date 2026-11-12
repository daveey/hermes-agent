"""Snapshot/feed reconciliation tolerates unrelated Board traffic."""
import pytest

from hermes_cli import kanban_priority_prepare as prepare
from tests.hermes_cli.test_kanban_priority_prepare import Pages
from hermes_cli.kanban_priority_feed import check_interval


class Feed:
    def __init__(self, page):
        self.page = page

    def changes(self, board, since):
        return self.page


def entry(**kwargs):
    return {'seq': 2, 'table': 'tasks', 'uid': 'a', 'op': 'upsert', 'fields': {}, **kwargs}


def one_page(change=None, **kwargs):
    return {'changes': [entry() if change is None else change], 'next': 2, 'more': False, **kwargs}


def raw_rows():
    return [{'id': 'a', 'status': 'ready', 'created_on': 'one', 'priority': 10000}]


@pytest.mark.parametrize('page', [
    one_page(changes=None), one_page(changes=[]), one_page(next=True), one_page(more=1),
    one_page(changes=[None]), one_page(entry(seq=True)), one_page(entry(seq=1)),
    one_page(entry(table=None)), one_page(next=3),
    one_page(entry(fields=None)), one_page(entry(fields={'priority': 10000.0})),
    one_page(entry(fields={'id': 'different'})), one_page(entry(fields={'status': 'invalid'})),
])
def test_feed_malformed_refuses(page):
    with pytest.raises(ValueError):
        check_interval(Feed(page), 'board', raw_rows(), 1, 2)


def test_feed_premature_end_refuses():
    with pytest.raises(ValueError, match='ended'):
        check_interval(Feed(one_page()), 'board', raw_rows(), 1, 3)


def test_feed_replays_open_status_and_ignores_after_fixed_target():
    rows = raw_rows()
    changes = [entry(fields={**rows[0], 'status': 'running'}),
               entry(seq=3, op='delete')]
    check_interval(Feed(one_page(changes=changes, next=3)), 'board', rows, 1, 2)
    assert rows[0]['status'] == 'running'


def test_feed_checks_first_change_after_first_head():
    class Churn:
        def changes(self, board, since):
            assert since == 1
            return one_page(entry(fields={'priority': 2}))
    with pytest.raises(ValueError, match='changed'):
        check_interval(Churn(), 'board', raw_rows(), 1, 2)


def test_feed_page_limit_is_bounded():
    class Endless:
        def changes(self, board, since):
            return one_page(entry(seq=since + 1, table='task_events'), next=since + 1, more=True)
    with pytest.raises(ValueError, match='page limit'):
        check_interval(Endless(), 'board', raw_rows(), 1, 10002)


class Busy(Pages):
    def snapshot_page(self, *args, **kwargs):
        page = super().snapshot_page(*args, **kwargs)
        page['head'] = 4 if kwargs.get('limit') == 1 else 3 if kwargs.get('after') else 1
        return page

    def changes(self, board, since):
        changes = [
            {'seq': 2, 'table': 'task_events', 'uid': 'event', 'op': 'upsert', 'fields': {}},
            {'seq': 3, 'table': 'tasks', 'uid': 'a', 'op': 'upsert', 'fields': {'title': 'updated'}},
            {'seq': 4, 'table': 'task_runs', 'uid': 'run', 'op': 'upsert', 'fields': {}},
        ]
        batch = [c for c in changes if c['seq'] > since][:2]
        return {'changes': batch, 'next': batch[-1]['seq'], 'more': batch[-1]['seq'] < 4}


def test_busy_capture_and_verify_ignore_unrelated_changes():
    manifest = prepare.capture(Busy(), board_id='board', exceptions=['b'])
    assert manifest['rows'][0]['old'] == 10000
    assert prepare.verify(Busy(), manifest, undo=True)['verified'] == 1


@pytest.mark.parametrize('fields', [{'priority': 2}, {'created_on': 'other'}, {'status': 'done'}])
def test_relevant_task_change_refuses(fields):
    class Changed(Busy):
        def changes(self, board, since):
            return {'changes': [{'seq': 4, 'table': 'tasks', 'uid': 'a', 'op': 'upsert', 'fields': fields}],
                    'next': 4, 'more': False}
    with pytest.raises(ValueError, match='changed'):
        prepare.capture(Changed(), board_id='board', exceptions=[])


@pytest.mark.parametrize('uid,op', [('a', 'delete'), ('new', 'upsert')])
def test_membership_change_refuses(uid, op):
    class Changed(Busy):
        def changes(self, board, since):
            return {'changes': [{'seq': 4, 'table': 'tasks', 'uid': uid, 'op': op, 'fields': {}}],
                    'next': 4, 'more': False}
    with pytest.raises(ValueError, match='changed'):
        prepare.capture(Changed(), board_id='board', exceptions=[])
