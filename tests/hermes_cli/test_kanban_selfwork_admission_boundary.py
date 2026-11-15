"""No declared creation may bypass the single-writer intake, even on key hits."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_selfwork_intake as intake
from tests.hermes_cli import test_kanban_selfwork as support
from tests.hermes_cli import test_kanban_selfwork_entry as entry_support

board = support.board
local = entry_support.local


def test_direct_creation_refused_before_key_replay(board):
    product = kb.create_task(board, title='Product', idempotency_key='existing-key')
    for key in (None, 'existing-key'):
        with pytest.raises(ValueError, match='intake'):
            kb.create_task(board, title='Fix', self_work=support.declaration(), idempotency_key=key)
    assert [t.id for t in kb.list_tasks(board)] == [product]


@pytest.mark.parametrize('entry', ['cli', 'tool'])
@pytest.mark.parametrize('value', [support.declaration(), support.declaration('guard')])
def test_unrouted_frontdoor_cannot_bypass_intake(local, entry, value):
    result = entry_support.create(entry, value)
    assert result == 2 if entry == 'cli' else not result.get('ok'), result
    assert not kb.list_tasks(local)


def test_intake_scope_does_not_leak_after_success_or_failure(board, monkeypatch):
    options = {'title': 'Fix', 'self_work': support.declaration()}
    result = intake.admit(board, request_id='boundary-001', authority='home', local_gateway='home',
                          options=options, now=1794690000)
    assert result['created'] is True
    original = kb.create_task
    def fail(*args, **kwargs):
        raise RuntimeError('insert failure')
    monkeypatch.setattr(kb, 'create_task', fail)
    with pytest.raises(RuntimeError, match='insert failure'):
        intake.admit(board, request_id='boundary-002', authority='home', local_gateway='home',
                     options={**options, 'self_work': support.declaration(area='other')}, now=1794690000)
    monkeypatch.setattr(kb, 'create_task', original)
    with pytest.raises(ValueError, match='intake'):
        kb.create_task(board, **options)
    assert len(kb.list_tasks(board)) == 1
    assert not board.execute('SELECT 1 FROM self_work_intake WHERE request_id=?', ('boundary-002',)).fetchone()


def test_created_callback_has_no_creation_capability(board):
    def callback(conn, task_id):
        with pytest.raises(ValueError, match='intake'):
            kb.create_task(conn, title='Unadmitted second fix', self_work=support.declaration(area='other'))
    intake.admit(board, request_id='boundary-003', authority='home', local_gateway='home',
                 options={'title': 'Fix', 'self_work': support.declaration()}, now=1794690000,
                 on_created=callback)
    assert len(kb.list_tasks(board)) == 1


def test_ordinary_product_creation_unchanged(local):
    for entry in ('cli', 'tool'):
        result = entry_support.create(entry, None)
        assert result == 0 if entry == 'cli' else result.get('ok'), result
    assert len(kb.list_tasks(local)) == 2
    assert all(support.policy.declared(local, task.id) is None for task in kb.list_tasks(local))


def test_guard_payload_requires_intake(board):
    with pytest.raises(ValueError, match='intake'):
        kb.create_task(board, title='New guard', self_work=support.declaration('guard'))
    assert not kb.list_tasks(board)


def test_origin_envelope_cannot_bypass_intake(board):
    from tests.hermes_cli.test_kanban_selfwork_intake import origin
    with pytest.raises(ValueError, match='intake'):
        kb.create_task(board, title='Unadmitted origin', self_work=support.declaration(),
                       origin_intent=origin('boundary-origin'))
    assert not kb.list_tasks(board)
    assert not board.execute("SELECT 1 FROM sqlite_master WHERE name='origin_creation_intents'").fetchone()


@pytest.mark.parametrize('mismatch', ['connection', 'declaration', 'transaction', 'thread'])
def test_capability_is_connection_declaration_transaction_and_thread_bound(board, mismatch):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    from contextlib import nullcontext
    value = support.declaration()
    other = sqlite3.connect(':memory:')
    try:
        with (nullcontext() if mismatch == 'transaction' else kb.write_txn(board)):
            with intake._creation_scope(board, value):
                target = other if mismatch == 'connection' else board
                declaration = support.declaration(area='other') if mismatch == 'declaration' else value
                with pytest.raises(ValueError, match='intake'):
                    if mismatch == 'thread':
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            pool.submit(copy_context().run, intake.assert_admitted_creation, target, declaration).result()
                    else:
                        intake.assert_admitted_creation(target, declaration)
    finally:
        other.close()
