"""Historical authority uses Board claims, not local lock bookkeeping."""
import json

import pytest

from hermes_cli import kanban_selfwork_forward as forward
from tests.hermes_cli import test_kanban_selfwork_forward as helpers

case = helpers.case


def _claim(tid, run_id, payload):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing(kb.kanban_db_path('default')) as conn:
        conn.execute("INSERT INTO task_events(task_id,run_id,kind,payload,created_at) VALUES(?,?,?,?,1)",
                     (tid, run_id, 'claimed', json.dumps(payload)))
        conn.commit()


@pytest.mark.parametrize('board_run_id', [None, 1])
def test_board_claim_survives_local_lock_receipt(case, tmp_path, monkeypatch, board_run_id):
    tid = helpers._seed_local_creator(tmp_path, monkeypatch, current=False)
    request = helpers._local_request(case, tid)
    helpers._journal_rejected(tmp_path, monkeypatch, request)
    _claim(tid, board_run_id, {'holder': 'worker-gw', 'run_uid': 'run', 'expires': 101000})
    _claim(tid, 1, {'lock': 'worker:123', 'expires': 900, 'run_id': 1})
    assert forward.receive(request)['code'] == 'self_work_forward_authorized'


@pytest.mark.parametrize('payload', [
    {'holder': 'other-gw', 'run_uid': 'run'},
    {'holder': 'worker-gw', 'run_uid': 'other'},
    {'run_uid': 'run'},
])
@pytest.mark.parametrize('board_run_id', [None, 1])
def test_wrong_board_authority_refused(case, tmp_path, monkeypatch, payload, board_run_id):
    tid = helpers._seed_local_creator(tmp_path, monkeypatch, current=False)
    request = helpers._local_request(case, tid)
    helpers._journal_rejected(tmp_path, monkeypatch, request)
    _claim(tid, board_run_id, payload)
    _claim(tid, 1, {'lock': 'worker:123', 'expires': 900, 'run_id': 1})
    with pytest.raises(ValueError, match='self_work_creator_unauthorized'):
        forward.receive(request)


def test_latest_exact_run_holder_vetoes_earlier_claim(case, tmp_path, monkeypatch):
    tid = helpers._seed_local_creator(tmp_path, monkeypatch, current=False, holder='worker-gw')
    request = helpers._local_request(case, tid)
    helpers._journal_rejected(tmp_path, monkeypatch, request)
    _claim(tid, None, {'holder': 'other-gw', 'run_uid': 'run'})
    with pytest.raises(ValueError, match='self_work_creator_unauthorized'):
        forward.receive(request)
