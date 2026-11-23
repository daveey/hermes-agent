"""Foreign comments retain the requesting gateway's classified writer, not a home relay's."""
import argparse
import hashlib
import json
from contextlib import nullcontext

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_scope_renewal_shadow as support

foreign = support.foreign
live = support.live


class Board:
    def __init__(self, task):
        self.task = task
        self.ident = support.support.SimpleNamespace(gateway='hub')
        self.rows = {}
        self.sent = []
        self.failure = None
        self.corrupt = False
        self.closed = False
        self.target_damage = None
        self.result_damage = None
        self.row_damage = None

    def ops(self, board, ops):
        assert self.closed  # the preflight connection was dropped before the nonreplayable POST
        self.sent.extend(ops)
        if self.failure == 'decode':
            raise json.JSONDecodeError('truncated response', '', 0)
        if self.failure == 'null':
            return None
        if self.failure == 'null-results':
            return {'results': None}
        if self.failure == 'null-entry':
            return {'results': [None, None]}
        if self.failure == 'transport':
            raise client.SyncUnavailable('lost reply')
        if self.failure == 'reject':
            return {'results': [{'client_op_id': op['client_op_id'], 'status': 'rejected'} for op in ops]}
        for op in ops:
            self.rows[op['table'], op['uid']] = {**op['fields'], 'uid': op['uid'], 'origin_gateway': 'hub', 'deleted_at': None}
        results = [{'client_op_id': op['client_op_id'], 'status': 'applied'} for op in ops]
        if self.result_damage == 'missing':
            results.pop()
        if self.result_damage == 'identity':
            results[0]['client_op_id'] = 'other'
        return {'results': results}

    def row_evidence(self, board, table, uid):
        if table == 'tasks':
            row = {'id': self.task, 'created_on': 'worker-home'}
            if self.target_damage == 'transport':
                raise client.SyncUnavailable('unavailable')
            if self.target_damage:
                row.update(self.target_damage)
            return {'state': 'live', 'row': row}
        row = self.rows.get((table, uid))
        if row and self.row_damage:
            row = {**row, **self.row_damage}
        if row and self.corrupt:
            row = {**row, 'origin_gateway': 'other'}
        return {'state': 'live', 'row': row}

    def close(self):
        self.closed = True


def invoke(monkeypatch, primary, tid):
    monkeypatch.setattr(kbc, 'connect_closing', lambda: nullcontext(primary))
    from hermes_cli import kanban_comment_writer as writer
    monkeypatch.setattr(writer, 'cli_comment_writer', lambda **kw: support.support.AGENT)
    return cli._cmd_comment(argparse.Namespace(task_id=tid, text=['New approved mandate'], max_len=None,
                                              author='default', origin_gateway=None))


def setup(monkeypatch, tid):
    board = Board(tid)
    monkeypatch.setattr(client, 'Hub', lambda *_: board)
    return board


def test_foreign_cli_comment_records_hub_writer_without_import(foreign, monkeypatch, capsys):
    primary, _, _, tid, _, _, path, _ = foreign
    board = setup(monkeypatch, tid)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert invoke(monkeypatch, primary, tid) == 0
    assert 'canonical comment' in capsys.readouterr().out
    assert not kb.get_task(primary, tid)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    comment, event = board.sent
    assert comment['table'] == 'task_comments' and event['table'] == 'task_events'
    assert {op['op'] for op in board.sent} == {'upsert'}
    payload = json.loads(event['fields']['payload'])
    assert payload['comment_uid'] == comment['uid']
    assert payload['writer_kind'] == 'agent'
    assert payload['writer_session'] == support.support.CHAT
    assert event['fields']['kind'] == 'commented'
    assert board.closed


@pytest.mark.parametrize('damage', [{'id': 'another'}, {'deleted_at': 1}, 'transport'])
def test_foreign_target_must_be_canonical_and_live(foreign, monkeypatch, capsys, damage):
    primary, _, _, tid, _, _, _, _ = foreign
    board = setup(monkeypatch, tid)
    board.target_damage = damage
    assert invoke(monkeypatch, primary, tid) == 2
    assert not board.sent
    assert 'Comment added' not in capsys.readouterr().out
    assert board.closed


@pytest.mark.parametrize('damage', ['missing', 'identity', {'uid': 'other'}, {'task_id': 'other'},
                                    {'body': 'changed'}, {'deleted_at': 1}])
def test_foreign_comment_requires_exact_ack_and_readback(foreign, monkeypatch, capsys, damage):
    primary, _, _, tid, _, _, _, _ = foreign
    board = setup(monkeypatch, tid)
    if isinstance(damage, str):
        board.result_damage = damage
    else:
        board.row_damage = damage
    assert invoke(monkeypatch, primary, tid) == 2
    assert 'Do not retry' in capsys.readouterr().err
    assert len(board.sent) == 2


@pytest.mark.parametrize('kind,session,task', [('human', None, None), ('agent', 'different', None),
                                             ('agent', 'worker-session', 't_worker')])
def test_foreign_comments_preserve_nonowner_identity(foreign, monkeypatch, kind, session, task):
    primary, _, _, tid, _, _, _, _ = foreign
    board = setup(monkeypatch, tid)
    from hermes_cli import kanban_foreign_comment as fc
    if task:
        monkeypatch.setenv('HERMES_KANBAN_WORKER_TASK', task)
    assert fc.append(primary, tid, 'default', 'note', writer_kind=kind, writer_session=session)
    payload = json.loads(board.sent[1]['fields']['payload'])
    assert payload.get('writer_kind') == kind
    assert payload.get('writer_session') == session
    assert payload.get('writer_task') == task


def test_foreign_comment_rejects_origin_override(foreign, monkeypatch):
    primary, _, _, tid, _, _, _, _ = foreign
    board = setup(monkeypatch, tid)
    from hermes_cli import kanban_foreign_comment as fc
    with pytest.raises(ValueError, match='cannot override'):
        fc.append(primary, tid, 'default', 'note', writer_kind='agent', writer_session='s', origin_gateway='other')
    assert not board.sent


def test_new_hub_mandate_survives_sync_and_foreign_renewal(foreign, monkeypatch, capsys):
    primary, home, renewal_hub, tid, episode, _, _, snapshot = foreign
    board = setup(monkeypatch, tid)
    assert invoke(monkeypatch, primary, tid) == 0
    capsys.readouterr()
    changes = []
    for (table, _), row in board.rows.items():
        remote = {**row, 'version': 1, 'updated_at': row['created_at'] * 1000,
                  'hlc': f"{row['created_at'] * 1000:013d}-0000-hub"}
        changes.append(client._row_change(table, remote))
    from hermes_cli import kanban_db_sync as sync
    sync.apply_remote_changes(home, changes, new_cursor=sync.sync_state(home)['cursor'], board='default')
    snapshot()
    comment_uid = board.sent[0]['uid']
    cid = home.execute('SELECT id FROM task_comments WHERE uid=?', (comment_uid,)).fetchone()[0]
    monkeypatch.setattr(client, 'Hub', lambda *_: renewal_hub)
    assert support.invoke(monkeypatch, primary, tid, episode, cid) == 0
    assert renewal_hub.sent[-1]['mandate_comment_uid'] == comment_uid
    assert renewal_hub.sent[-1]['payload']['actor_session'] == support.support.CHAT
    assert not kb.get_task(primary, tid)
    assert kb.get_task(home, tid).status == 'triage'


@pytest.mark.parametrize('failure', ['reject', 'transport', 'readback', 'decode', 'null', 'null-results', 'null-entry'])
def test_foreign_comment_never_claims_success_on_uncertain_or_rejected_delivery(foreign, monkeypatch, capsys, failure):
    primary, _, _, tid, _, _, path, _ = foreign
    board = setup(monkeypatch, tid)
    board.failure = failure
    board.corrupt = failure == 'readback'
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert invoke(monkeypatch, primary, tid) == 2
    output = capsys.readouterr()
    assert 'Comment added' not in output.out
    assert 'Do not retry' in output.err
    assert len(board.sent) == 2  # one submission, no replay or home relay
    assert not kb.get_task(primary, tid)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert board.closed
