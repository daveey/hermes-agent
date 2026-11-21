"""Real core writes behind independently read request records; no network writes."""
import copy

import pytest

from hermes_cli import kanban_card_answer as ca
from hermes_cli import kanban_card_answer_runtime as runtime
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_card_answer as helpers
from tests.hermes_cli import test_kanban_card_answer_proof as proof_helpers

kanban_home = helpers.kanban_home


def setup(monkeypatch, home):
    with kbc.connect_closing() as conn:
        answer = helpers.setup_answer(conn)
        conn.execute('CREATE TABLE kanban_sync_state(gateway_id TEXT, mode TEXT)')
        conn.execute("INSERT INTO kanban_sync_state VALUES ('owner','primary')")
        conn.commit()
    _, rows = proof_helpers.records()
    for row in rows.values():
        row['args']['answer'] = copy.deepcopy(answer)
    rows['original']['key'] = 'default/default/' + answer['task']
    rows['proof']['result']['answer'] = copy.deepcopy(answer)
    calls = []
    class Records:
        gateway = 'owner'
        def read(self, rid):
            calls.append(rid)
            return copy.deepcopy(rows[rid])
    monkeypatch.setattr(runtime, 'Records', Records)
    monkeypatch.setattr(runtime, '_profile', lambda: ('default', home))
    return answer, rows, calls


def test_owner_applies_readback_and_replays_without_proof(kanban_home, monkeypatch):
    answer, rows, calls = setup(monkeypatch, kanban_home)
    receipt = runtime.owner('original', 'proof')
    assert receipt['operationId'] == answer['operationId']
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, answer['task']).status == 'ready'
        assert len(helpers.comments(conn, answer['task'])) == 1
    del rows['proof']
    calls.clear()
    assert runtime.owner('original', 'proof') == receipt
    assert calls == ['original']


@pytest.mark.parametrize('field,value', [('ownerGateway', 'foreign'), ('ownerProfile', 'work'),
                                        ('board', '../default'), ('board', 'absent')])
def test_route_refuses_without_mutation(kanban_home, monkeypatch, field, value):
    answer, rows, _ = setup(monkeypatch, kanban_home)
    rows['original']['args']['answer'][field] = value
    with pytest.raises((ca.AnswerRefused, ValueError)):
        runtime.owner('original', 'proof')
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, answer['task']).status == 'blocked'
        assert helpers.comments(conn, answer['task']) == []


@pytest.mark.parametrize('where', ['before', 'during'])
def test_copied_foreign_card_refuses_under_lock(kanban_home, where):
    with kbc.connect_closing() as conn:
        req = helpers.setup_answer(conn)
        conn.execute('ALTER TABLE tasks ADD COLUMN created_on TEXT')
        def foreign():
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET created_on='foreign' WHERE id=?", (req['task'],))
        if where == 'before':
            foreign()
        def verify(a):
            if where == 'during':
                foreign()
            return helpers.verified(a)
        with pytest.raises(ca.AnswerRefused, match='owner_card_missing_or_foreign'):
            helpers.apply(conn, req, verify)
        assert kb.get_task(conn, req['task']).status == 'blocked'
        assert helpers.comments(conn, req['task']) == []


@pytest.mark.parametrize('gateway,mode', [('foreign', 'primary'), ('owner', 'replica')])
def test_wrong_board_identity_refuses(kanban_home, gateway, mode):
    with kbc.connect_closing() as conn:
        req = helpers.setup_answer(conn)
        conn.execute('CREATE TABLE kanban_sync_state(gateway_id TEXT, mode TEXT)')
        conn.execute('INSERT INTO kanban_sync_state VALUES (?,?)', (gateway, mode))
        conn.commit()
        with pytest.raises(ca.AnswerRefused, match='owner_board_not_primary'):
            helpers.apply(conn, req)
        assert kb.get_task(conn, req['task']).status == 'blocked'


@pytest.mark.parametrize('archive', [False, True])
def test_containment_observation_without_marker_preserved(kanban_home, archive):
    with kbc.connect_closing() as conn:
        req = helpers.setup_answer(conn, choices=not archive)
        if archive:
            req['choice'] = 'archive'
        conn.execute('CREATE TABLE containment_observations(task_id TEXT, episode TEXT)')
        conn.execute("INSERT INTO containment_observations VALUES (?, 'unverified-episode')", (req['task'],))
        conn.commit()
        with pytest.raises(ca.AnswerRefused, match='ask_changed_or_held'):
            helpers.apply(conn, req)
        assert kb.get_task(conn, req['task']).status == 'blocked'
        assert helpers.comments(conn, req['task']) == []


def test_runtime_rejects_board_without_primary_identity(kanban_home, monkeypatch):
    answer, _, _ = setup(monkeypatch, kanban_home)
    with kbc.connect_closing() as conn:
        conn.execute('DROP TABLE kanban_sync_state')
        conn.commit()
    with pytest.raises(ca.AnswerRefused, match='owner_board_not_primary'):
        runtime.owner('original', 'proof')
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, answer['task']).status == 'blocked'


def test_source_reads_local_store_only_after_both_authenticated_legs(monkeypatch, tmp_path):
    answer, rows = proof_helpers.records()
    rows['proof'].pop('result')
    rows['proof']['state'] = 'pending'
    calls = []
    class Records:
        gateway = 'origin'
        def read(self, rid):
            calls.append(rid)
            return copy.deepcopy(rows[rid])
    monkeypatch.setattr(runtime, 'Records', Records)
    monkeypatch.setattr(runtime, '_profile', lambda: ('work', tmp_path))
    def read(path, **kwargs):
        assert calls == ['proof', 'original']
        assert path == tmp_path / 'user-steps/actions.sqlite3'
        assert kwargs['answer'] == answer
        return {'proof': 'from-real-reader'}
    monkeypatch.setattr(runtime, 'recorded', read)
    assert runtime.source('proof') == {'proof': 'from-real-reader'}
    rows['original']['from'] = 'gw:foreign'
    with pytest.raises(ca.AnswerRefused, match='source_request_mismatch'):
        runtime.source('proof')
