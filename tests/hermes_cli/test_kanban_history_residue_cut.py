"""Fresh same-head observations cannot be cached as sync-diff exemptions."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_history_residue_custody as custody
from hermes_cli import kanban_history_residue_cut as cut
from hermes_cli import kanban_history_residue_local as local
from hermes_cli.kanban_history_residue_source import Refused
from tests.hermes_cli import test_kanban_history_residue_local as fixtures
from tests.hermes_cli.test_kanban_history_residue_proof import proof

stores = fixtures.stores


@pytest.fixture
def cut_inputs(stores, monkeypatch):
    live, shadow, raw = stores
    db = sqlite3.connect(':memory:', isolation_level=None)
    _, data = proof()
    parent = local.snapshot(live, raw, board='default', gateway='home', replica=False)['portable']
    data['receipt']['source_json'] = raw
    data['current']['parent'].update(parent)
    request = json.dumps(dict(version=1, task='t_stopped', expected_version=7, source_json=raw, reason='Keep history'))
    ident = SimpleNamespace(url='https://board.example', gateway='home', key='key')
    hub = SimpleNamespace(ident=ident)
    custody.stage(db, ('https://board.example','default','home'), request)
    custody.seal(db, ('https://board.example','default','home'), 'op-original', json.dumps(data['receipt']))
    calls = []

    def changes(board, head):
        assert not live.in_transaction and not shadow.in_transaction
        calls.append('feed')
        return dict(head=100, next=100, more=False, changes=[])

    def current(*args):
        assert not live.in_transaction and not shadow.in_transaction
        calls.append('current')
        return json.dumps(data)

    hub.changes = changes
    monkeypatch.setattr(cut.transport, 'current', current)
    try:
        yield live, shadow, db, hub, data, calls
    finally:
        db.close()


def verify(cut_inputs):
    live, shadow, db, hub, _, _ = cut_inputs
    return cut.verify(live, shadow, db, hub, 'default', ['op-original'], started=cut.time.monotonic())


def test_fresh_cut_readback_and_no_writes(cut_inputs):
    live, shadow, db, _hub, data, calls = cut_inputs
    before = [list(c.iterdump()) for c in (live, shadow, db)]
    report = verify(cut_inputs)
    assert calls == ['feed','current','feed']
    assert report['head'] == 100 and report['audited_history_rejections'] == ['op-original']
    assert report['diff']['clean'] is True and report['diff']['rejected_ops'] == 1
    assert report['evidence'][0]['source_json'] == data['receipt']['source_json']
    assert before == [list(c.iterdump()) for c in (live, shadow, db)]
    assert not live.in_transaction and not shadow.in_transaction


@pytest.mark.parametrize('fault', ['parent','head','receipt','missing','unsealed','busy','expired','local-race','replica-race','moving-head'])
def test_invalid_proof_is_not_an_audit(cut_inputs, monkeypatch, fault):
    live, shadow, db, hub, data, _calls = cut_inputs
    if fault == 'parent': data['current']['parent']['title'] = 'other'
    if fault == 'head': data['current']['observed_head'] = 101
    if fault == 'receipt': data['receipt']['reason'] = 'different'
    if fault == 'missing': db = sqlite3.connect(':memory:', isolation_level=None)
    if fault == 'unsealed':
        db = sqlite3.connect(':memory:', isolation_level=None)
        custody.stage(db, ('https://board.example','default','home'), json.dumps(dict(version=1,
            task='t_stopped', expected_version=7, source_json=data['receipt']['source_json'], reason='Keep history')))
    if fault == 'busy': live.execute('BEGIN')
    if fault == 'expired': monkeypatch.setattr(cut, 'MAX_AGE', -1)
    old = cut.transport.current

    def changed(*args):
        value = old(*args)
        if fault == 'local-race': live.execute("UPDATE tasks SET worker_pid=77")
        if fault == 'replica-race': shadow.execute("UPDATE tasks SET title='race'")
        if fault == 'moving-head': hub.changes = lambda *a: dict(head=101,next=101,more=False,changes=[])
        return value

    monkeypatch.setattr(cut.transport, 'current', changed)
    try:
        with pytest.raises(Refused):
            cut.verify(live, shadow, db, hub, 'default', ['op-original'], started=cut.time.monotonic())
        if fault != 'busy':
            assert not live.in_transaction and not shadow.in_transaction
    finally:
        if fault == 'busy': live.rollback()
        if fault in ('missing','unsealed'): db.close()


def test_saved_cut_does_not_change_ordinary_diff(cut_inputs):
    from hermes_cli.kanban_sync_client import diff
    live, shadow, _db, _hub, _data, _calls = cut_inputs
    assert verify(cut_inputs)['diff']['clean'] is True
    assert diff(live, shadow, 'home')['clean'] is False
    assert diff(live, shadow, 'home')['rejected_ops'] == 1


@pytest.mark.parametrize('during_http', [False, True])
def test_absent_run_omission_cannot_clean_cut(cut_inputs, monkeypatch, during_http):
    live, _shadow, _db, _hub, _data, _calls = cut_inputs

    def insert():
        live.execute("INSERT INTO kanban_sync_omissions VALUES ('task_runs','gone','ended_at','held')")

    if during_http:
        old = cut.transport.current

        def current(*args):
            result = old(*args)
            insert()
            return result

        monkeypatch.setattr(cut.transport, 'current', current)
    else:
        insert()
    with pytest.raises(Refused, match='unattributed_run_work'):
        verify(cut_inputs)


def test_unrelated_rejection_is_not_subtracted(cut_inputs):
    live, _shadow, _db, _hub, _data, _calls = cut_inputs
    live.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,state) VALUES ('unknown','tasks','unrelated','rejected')")
    report = verify(cut_inputs)['diff']
    assert report['rejected_ops'] == 2 and report['clean'] is False


def test_ordinary_row_drift_still_blocks_audited_cut(cut_inputs):
    live, shadow, _db, _hub, _data, _calls = cut_inputs
    for conn in (live, shadow):
        conn.execute("INSERT INTO tasks(id,title,created_on,sync_version) VALUES ('unrelated','same','home',1)")
    live.execute("UPDATE tasks SET title='different' WHERE id='unrelated'")
    report = verify(cut_inputs)['diff']
    assert report['rejected_ops'] == 1 and report['clean'] is False
    assert report['field_mismatch'] == {'unrelated': ['title']}


def test_expiry_during_final_snapshot_cannot_emit_report(cut_inputs, monkeypatch):
    original = cut._capture
    calls = []

    def delayed(*args):
        result = original(*args)
        calls.append(1)
        if len(calls) == 2:
            monkeypatch.setattr(cut, 'MAX_AGE', -1)
        return result

    monkeypatch.setattr(cut, '_capture', delayed)
    with pytest.raises(Refused, match='evidence_expired'):
        verify(cut_inputs)


def test_real_second_writer_refuses_cut_without_waiting(cut_inputs, tmp_path, monkeypatch):
    live, shadow, ledger, hub, _data, _calls = cut_inputs
    path = tmp_path / 'primary.db'
    other = sqlite3.connect(path, isolation_level=None)
    live.backup(other)
    file_live = sqlite3.connect(path, isolation_level=None)
    file_live.row_factory = sqlite3.Row
    old = cut.transport.current

    def writer(*args):
        value = old(*args)
        other.execute('BEGIN IMMEDIATE')
        return value

    monkeypatch.setattr(cut.transport, 'current', writer)
    try:
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            cut.verify(file_live, shadow, ledger, hub, 'default', ['op-original'], started=cut.time.monotonic())
        assert not file_live.in_transaction and not shadow.in_transaction
        assert file_live.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
    finally:
        other.rollback()
        other.close()
        file_live.close()
