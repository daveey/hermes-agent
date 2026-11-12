"""Capture/verify replay the feed from the FIRST snapshot head, never a later one.

Busy-style fixtures whose relevant task changes land between the first page
head and a later page head (seq 2/3) must refuse; a page head that regresses
below the first head is a malformed snapshot. The real ``check_interval`` feed
reconciliation runs in every case: only the hub's ``changes`` calls are recorded.
"""
import pytest

from hermes_cli import kanban_priority_prepare as prepare
from tests.hermes_cli.test_kanban_priority_feed import Busy
from tests.hermes_cli.test_kanban_priority_prepare import Pages


def _change(seq, table='task_events', uid='event', op='upsert', fields=None):
    return {'seq': seq, 'table': table, 'uid': uid, 'op': op, 'fields': fields or {}}


class Between(Busy):
    """Busy heads (first 1, last page 3, final 4); one relevant change at seq 2 or 3."""

    def __init__(self, relevant=None):
        super().__init__()
        self.relevant = relevant
        self.since = []

    def changes(self, board, since):
        self.since.append(since)
        log = [_change(2), _change(3, table='tasks', uid='a', fields={'title': 'updated'}),
               _change(4, table='task_runs', uid='run')]
        if self.relevant is not None:
            log[self.relevant['seq'] - 2] = self.relevant
        batch = [c for c in log if c['seq'] > since][:2]
        return {'changes': batch, 'next': batch[-1]['seq'], 'more': batch[-1]['seq'] < 4}


RELEVANT = [
    pytest.param({'table': 'tasks', 'uid': 'a', 'fields': {'priority': 2}}, id='priority'),
    pytest.param({'table': 'tasks', 'uid': 'a', 'fields': {'created_on': 'other'}}, id='origin'),
    pytest.param({'table': 'tasks', 'uid': 'a', 'fields': {'status': 'done'}}, id='terminal'),
    pytest.param({'table': 'tasks', 'uid': 'a', 'op': 'delete'}, id='delete'),
    pytest.param({'table': 'tasks', 'uid': 'new'}, id='insert'),
]


def _relevant(seq, spec):
    return _change(seq, **spec)


def test_legitimate_quiet_and_busy_captures_stay_accepted():
    assert prepare.capture(Pages(), board_id='board', exceptions=['b'])['rows'][0]['old'] == 10000
    hub = Between()
    manifest = prepare.capture(hub, board_id='board', exceptions=['b'])
    assert manifest['rows'][0]['old'] == 10000
    # Pinned: replay starts at the first page head (1), through the final head (4).
    assert hub.since == [1, 3]
    verifier = Between()
    assert prepare.verify(verifier, manifest, undo=True)['verified'] == 1
    assert verifier.since == [1, 3]


@pytest.mark.parametrize('seq', [2, 3])
@pytest.mark.parametrize('spec', RELEVANT)
def test_capture_refuses_change_between_first_and_last_page_head(seq, spec):
    hub = Between(_relevant(seq, spec))
    with pytest.raises(ValueError, match='changed'):
        prepare.capture(hub, board_id='board', exceptions=['b'])
    assert hub.since[0] == 1


@pytest.mark.parametrize('seq', [2, 3])
@pytest.mark.parametrize('spec', RELEVANT)
def test_verify_refuses_change_between_first_and_last_page_head(seq, spec):
    manifest = prepare.capture(Between(), board_id='board', exceptions=['b'])
    hub = Between(_relevant(seq, spec))
    with pytest.raises(ValueError, match='changed'):
        prepare.verify(hub, manifest, undo=True)
    assert hub.since[0] == 1


class Regressing(Busy):
    """Malformed snapshot: a later page head (2) below the first page head (3)."""

    def snapshot_page(self, *args, **kwargs):
        page = super().snapshot_page(*args, **kwargs)
        page['head'] = 4 if kwargs.get('limit') == 1 else 2 if kwargs.get('after') else 3
        return page


def test_regressing_page_head_refuses_capture_and_verify():
    with pytest.raises(ValueError, match='changed'):
        prepare.capture(Regressing(), board_id='board', exceptions=['b'])
    manifest = prepare.capture(Busy(), board_id='board', exceptions=['b'])
    with pytest.raises(ValueError, match='changed'):
        prepare.verify(Regressing(), manifest, undo=True)
