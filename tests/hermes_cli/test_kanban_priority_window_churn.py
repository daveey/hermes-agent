"""Open<->terminal churn inside the paged capture window replays to the final head.

A paged snapshot is not taken at one head: a row paged late can already show a
later state than an intermediate feed change (2026-11-13 MacBook capture, seq
1725387: snapshot ``done``, replayed upsert ``ready``). Capture and verify must
compare the FINAL replayed state with the snapshot rows, so a card finishing
or reopening mid-window no longer refuses. Priority, origin, identity, deletes
and open cards missing from the snapshot still refuse.
"""
import pytest

from hermes_cli import kanban_priority_prepare as prepare
from hermes_cli.kanban_priority_feed import check_interval


def _row(key, status='ready', priority=10000, origin='one'):
    return {'id': key, 'status': status, 'created_on': origin, 'priority': priority}


def _task(seq, uid, op='upsert', **fields):
    return {'seq': seq, 'table': 'tasks', 'uid': uid, 'op': op, 'fields': fields}


class Window:
    """Two snapshot pages (heads 1 then 3), a final head of 5, and a task feed."""

    def __init__(self, rows, changes):
        self.rows = rows
        self.log = [{'seq': s, 'table': 'task_events', 'uid': 'e', 'op': 'upsert', 'fields': {}}
                    for s in range(2, 6)]
        for change in changes:
            self.log[change['seq'] - 2] = change
        self.since = []

    def snapshot_page(self, board, table, after='', limit=1000):
        if limit == 1:
            return {'table': 'tasks', 'head': 5, 'rows': self.rows[:1], 'more': True,
                    'next': self.rows[0]['id']}
        half = (len(self.rows) + 1) // 2
        batch = self.rows[half:] if after else self.rows[:half]
        return {'table': 'tasks', 'head': 3 if after else 1, 'rows': batch, 'more': not after,
                'next': batch[-1]['id'] if batch else after}

    def changes(self, board, since):
        self.since.append(since)
        batch = [c for c in self.log if c['seq'] > since][:2]
        return {'changes': batch, 'next': batch[-1]['seq'], 'more': batch[-1]['seq'] < 5}


def _ids(manifest):
    return [row['id'] for row in manifest['rows']]


@pytest.mark.parametrize('terminal', ['done', 'archived'])
def test_card_created_then_completed_mid_window_absent_from_snapshot(terminal):
    hub = Window([_row('a'), _row('b')], [_task(2, 'z', **_row('z')), _task(4, 'z', status=terminal)])
    assert _ids(prepare.capture(hub, board_id='board', exceptions=[])) == ['a', 'b']
    assert hub.since[0] == 1


def test_card_created_then_completed_mid_window_paged_late_as_done():
    # The incident shape: the late page already shows done, the replay has ready.
    hub = Window([_row('a'), _row('z', status='done')],
                 [_task(2, 'z', **_row('z')), _task(3, 'z', status='ready'),
                  _task(4, 'z', status='done')])
    assert _ids(prepare.capture(hub, board_id='board', exceptions=[])) == ['a']


@pytest.mark.parametrize('snapshot', ['ready', 'done'], ids=['paged-before', 'paged-after'])
def test_existing_card_completed_mid_window(snapshot):
    rows = [_row('a', status=snapshot), _row('b')]
    hub = Window(rows, [_task(4, 'a', **{**_row('a'), 'status': 'done'})])
    assert _ids(prepare.capture(hub, board_id='board', exceptions=[])) == ['b']
    assert rows[0]['status'] == 'done'


def test_existing_card_reopened_mid_window_joins_manifest():
    rows = [_row('a', status='done', priority=7), _row('b')]
    hub = Window(rows, [_task(4, 'a', status='ready')])
    manifest = prepare.capture(hub, board_id='board', exceptions=[])
    assert [(r['id'], r['old']) for r in manifest['rows']] == [('a', 7), ('b', 10000)]


def test_open_status_patch_without_status_keeps_replayed_terminal_state():
    rows = [_row('a'), _row('b')]
    hub = Window(rows, [_task(2, 'a', status='done'), _task(4, 'a', title='renamed')])
    assert _ids(prepare.capture(hub, board_id='board', exceptions=[])) == ['b']


def test_statusless_patch_keeps_snapshot_status():
    rows = [_row('a', status='done'), _row('b', status='blocked')]
    hub = Window(rows, [_task(3, 'a', title='renamed'), _task(4, 'b', title='renamed')])
    assert _ids(prepare.capture(hub, board_id='board', exceptions=[])) == ['b']
    assert [row['status'] for row in rows] == ['done', 'blocked']


def test_completion_mid_window_resolves_terminal_exception():
    hub = Window([_row('a'), _row('b')], [_task(3, 'a', status='archived')])
    manifest = prepare.capture(hub, board_id='board', exceptions=['a'])
    assert manifest['exceptions'] == [{'id': 'a', 'origin': 'one', 'old': 10000, 'status': 'archived'}]


def test_verify_accepts_manifest_row_completed_mid_window():
    manifest = prepare.capture(Window([_row('a'), _row('b')], []), board_id='board', exceptions=[])
    hub = Window([_row('a'), _row('b')], [_task(2, 'a', status='done')])
    result = prepare.verify(hub, manifest, undo=True)
    assert result['verified'] == 1


@pytest.mark.parametrize('seq', [2, 3, 4, 5])
@pytest.mark.parametrize('fields', [
    pytest.param({'priority': 2}, id='priority'),
    pytest.param({'priority': 2, 'status': 'done'}, id='priority-then-terminal'),
    pytest.param({'created_on': 'other'}, id='origin'),
    pytest.param({'id': 'other'}, id='identity'),
])
def test_real_change_mid_window_still_refuses(seq, fields):
    hub = Window([_row('a'), _row('b')], [_task(seq, 'a', **fields)])
    with pytest.raises(ValueError, match='changed'):
        prepare.capture(hub, board_id='board', exceptions=[])


def test_priority_change_before_completion_still_refuses_against_late_done_row():
    hub = Window([_row('a'), _row('z', status='done')],
                 [_task(2, 'z', **_row('z', priority=4)), _task(4, 'z', status='done')])
    with pytest.raises(ValueError, match='changed'):
        prepare.capture(hub, board_id='board', exceptions=[])


@pytest.mark.parametrize('changes', [
    pytest.param([_task(2, 'z', **_row('z'))], id='created-open'),
    pytest.param([_task(2, 'z', **_row('z')), _task(3, 'z', status='done'),
                  _task(4, 'z', status='ready')], id='created-done-reopened'),
    pytest.param([_task(2, 'z', title='no status')], id='unknown-status'),
    pytest.param([_task(2, 'z', status='done'), _task(4, 'z', op='delete')], id='missing-delete'),
    pytest.param([_task(3, 'a', op='delete')], id='delete'),
    pytest.param([_task(3, None, status='done')], id='no-uid'),
])
def test_membership_change_mid_window_still_refuses(changes):
    hub = Window([_row('a'), _row('b')], changes)
    with pytest.raises(ValueError, match='changed'):
        prepare.capture(hub, board_id='board', exceptions=[])


def test_changes_after_final_head_are_ignored():
    rows = [_row('a')]

    class Feed:
        def changes(self, board, since):
            return {'changes': [_task(2, 'a', status='done'), _task(3, 'z', **_row('z'))],
                    'next': 3, 'more': False}

    check_interval(Feed(), 'board', rows, 1, 2)
    assert rows[0]['status'] == 'done'


@pytest.mark.parametrize('status', [None, ['done'], 'invalid'])
def test_invalid_replayed_status_refuses(status):
    with pytest.raises(ValueError, match='changed'):
        check_interval(Window([_row('a')], [_task(2, 'z', status=status)]), 'board', [_row('a')], 1, 5)
