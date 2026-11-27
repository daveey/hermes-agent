"""A forwarded queued create returns an executable, lossless park handle."""
import json
import shlex

import pytest

from tools import kanban_tools as kt
from hermes_cli import kanban_creation_home as home
from hermes_cli import kanban_placement as placement
from tests.hermes_cli import test_kanban_wait_replica as fixtures

board = fixtures.board
KEY = "core-wt-review:softmax-universe/daveey-hermes#4557:2384f69287c132d5aabe90c7a56d58c6257b2bce"


@pytest.fixture(params=[False, True], ids=['legacy-route', 'canonical-home'])
def queued(monkeypatch, request):
    monkeypatch.setattr(home, 'prepare', lambda view, **kw:
                        (True if request.param else None, {**view, 'runs_on': ['pool:worker']},
                         {'where': 'hub', 'why': 'test'}))
    monkeypatch.setattr(kt, '_forwarding_fields', lambda *a: {})
    monkeypatch.setattr(placement, 'create_remote', lambda *a, **kw: {
        'ok': True, 'request_id': 'request-queued', 'queued_only': True})

    def create(key):
        args = {'title': 'Review queued change'}
        if key is not None:
            args['idempotency_key'] = key
        raw, _ = kt._place_remote(args, assignee='default', skills=None, parents=None,
                                  project_id=None, session_id=None)
        out = json.loads(raw)
        assert out['status'] == 'queued', out
        return out
    return create


@pytest.mark.parametrize('key', [KEY, "review:quotes ' and spaces ; $(false)"])
def test_queued_handle(queued, key):
    out = queued(key)
    assert out['wake_check'] == 'hermes kanban wait-check key ' + shlex.quote(key) + ' --gateway hub'
    assert shlex.split(out['wake_check']) == ['hermes', 'kanban', 'wait-check', 'key', key, '--gateway', 'hub']
    assert 'using exactly this wake_check' in out['note']
    assert 'never a constant probe (`sleep 1; exit 1`) or a clock_only park' in out['note']


def test_keyless_queue_has_bounded_lookup_note(queued):
    out = queued(None)
    assert 'wake_check' not in out
    assert 'at most 2 h without a probe' in out['note']
    assert 'hermes-hub requests' in out['note']


def test_schedule_stores_returned_handle_unchanged(queued, board, monkeypatch):
    from hermes_cli import kanban_db as kb
    conn, tid = board
    cmd = queued(KEY)['wake_check']
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(kb.get_task(conn, tid).current_run_id))
    monkeypatch.setenv('HERMES_KANBAN_WAKE_DRYRUN', '0')
    out = json.loads(kt._handle_schedule({
        'reason': 'SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting for queued review',
        'wake_check': cmd, 'check_every': 120,
        'awaiting': {'kind': 'external', 'ref': 'request-queued', 'why': 'hub courier creates the review'},
    }))
    assert out.get('status') == 'scheduled', out
    assert conn.execute('SELECT cmd FROM task_wake_checks WHERE task_id=?', (tid,)).fetchone()[0] == cmd
