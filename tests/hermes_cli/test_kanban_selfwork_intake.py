"""Transactional authority and origin guarantees."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_selfwork_intake as intake
from tests.hermes_cli import test_kanban_selfwork as support

board = support.board


def origin(request, session='s_chat'):
    return {'request_id': request, 'owner': {'gateway': 'home', 'profile': 'default', 'board': 'default'},
            'origin': {'gateway': 'desktop', 'profile': 'default', 'session': session}}


def admit(conn, request='request-0001', **options):
    return intake.admit(conn, request_id=request, authority='home', local_gateway='home', now=1794690000,
                        options={'title': 'Finding', 'self_work': support.declaration(), **options})


def test_concurrent_findings_share_owner(board):
    from hermes_cli.kanban_db_connect import connect_closing
    barrier = Barrier(2)
    def create(request):
        with connect_closing() as conn:
            barrier.wait(timeout=10)
            return admit(conn, request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ['request-0001', 'request-0002']))
    assert {r['disposition'] for r in results} == {'create', 'append'}
    assert len({r['task_id'] for r in results}) == 1


def test_legacy_key_cannot_adopt_product(board):
    kb.create_task(board, title='Product', idempotency_key='shared')
    with pytest.raises(ValueError, match='idempotency'):
        admit(board, idempotency_key='shared')
    assert len(kb.list_tasks(board)) == 1


def test_origin_adoption_and_replay(board):
    first = admit(board, origin_intent=origin('origin-0001'))
    second = admit(board, 'request-0002', origin_intent=origin('origin-0002', 's_second'))
    third = admit(board, 'request-0003', origin_intent=origin('origin-0003', 's_second'))
    assert first['task_id'] == second['task_id'] == third['task_id']
    assert third['origin_creation'] == second['origin_creation']
    assert len(kb.list_comments(board, first['task_id'])) == 2


def test_tenants_do_not_merge(board):
    first = admit(board, tenant='a')
    second = admit(board, 'request-0002', tenant='b')
    assert first['task_id'] != second['task_id']


def test_receipt_failure_is_atomic(board, monkeypatch):
    first = admit(board)
    def fail(*args):
        raise RuntimeError('receipt failed')
    monkeypatch.setattr(intake, '_store', fail)
    with pytest.raises(RuntimeError, match='receipt failed'):
        admit(board, 'request-0002')
    assert not kb.list_comments(board, first['task_id'])


def test_origin_must_name_authority(board):
    wrong = origin('origin-0001')
    wrong['owner']['gateway'] = 'worker'
    with pytest.raises(ValueError, match='authority'):
        admit(board, origin_intent=wrong)
    assert not kb.list_tasks(board)
