"""Producer/receipt fixtures, not a real courier or owner-transcript delivery."""
from copy import deepcopy
from pathlib import Path

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_notify_reconcile_exec as notices
from hermes_cli import kanban_replay_diagnostics as diagnostics
from hermes_cli import kanban_replay_journal as journal
from tests.hermes_cli import test_kanban_replay_episodes as helpers

live = helpers.live
clock = helpers.clock


def due(conn, clock):
    receiver = helpers.setup(conn)
    helpers.helpers.poll(conn, receiver)
    assert diagnostics.pending(conn) == []
    for _ in range(5):
        clock[1] += 60000
        helpers.helpers.poll(conn, receiver)
    rows = diagnostics.pending(conn)
    assert len(rows) == 1
    return receiver, rows[0]


def readback(read, delivered=True):
    return dict(state='done', body=dict(id=read['id'], to_gw=read['to_gw'], coll='sessions',
                key=f"{read['profile']}/{read['chat']}", action=notices.ACTION,
                args=dict(keys=','.join(read['keys']))),
                result=dict(code='notice_receipts', profile=read['profile'], session=read['chat'],
                            keys=read['keys'], session_exists=True,
                            delivered=read['keys'] if delivered else []))


def bind(conn, key):
    return diagnostics.bind_owner(conn, key, gateway='fixture-owner-gw', profile='default', chat='fixture-owner-chat')


def test_actual_observer_produces_one_key_across_restart_without_receipt(live, clock, monkeypatch):
    receiver, row = due(live, clock)
    key = row['notice_key']
    read = bind(live, key)
    state = journal.snapshot(live)
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    live.close()
    conn = kbc.connect(path)
    try:
        monkeypatch.setattr(notices, '_sent', lambda ident: None)
        assert not diagnostics.record_delivery(conn, key)
        clock[0] = 'process-after-restart'
        for _ in range(2):
            clock[1] += 60000
            helpers.helpers.poll(conn, receiver)
        rows = diagnostics.pending(conn)
        assert len(rows) == 1 and rows[0]['notice_key'] == key
        assert bind(conn, key) == read
        assert journal.snapshot(conn) == state
        assert rows[0]['receipt_json'] is None
    finally:
        conn.close()


@pytest.mark.parametrize('state', ['queued', 'sent', 'running', 'rejected', 'unknown'])
def test_transport_flags_never_mean_delivered(live, clock, monkeypatch, state):
    _, row = due(live, clock)
    key = row['notice_key']
    read = bind(live, key)
    sent = readback(read)
    sent['state'] = state
    monkeypatch.setattr(notices, '_sent', lambda ident: sent)
    assert not diagnostics.record_delivery(live, key)
    assert diagnostics.pending(live)[0]['receipt_json'] is None


@pytest.mark.parametrize('defect', ['not_delivered', 'missing_session', 'wrong_key', 'wrong_owner', 'wrong_request'])
def test_only_matching_owner_transcript_receipt_releases_pending(live, clock, monkeypatch, defect):
    _, row = due(live, clock)
    key = row['notice_key']
    read = bind(live, key)
    sent = readback(read, delivered=defect != 'not_delivered')
    mutations = {
        'missing_session': lambda: sent['result'].update(session_exists=False),
        'wrong_key': lambda: sent['result'].update(keys=['another-episode']),
        'wrong_owner': lambda: sent['body'].update(to_gw='foreign-owner'),
        'wrong_request': lambda: sent['body'].update(id='foreign-request'),
    }
    mutations.get(defect, lambda: None)()
    monkeypatch.setattr(notices, '_sent', lambda ident: sent)
    if defect in ('not_delivered', 'missing_session'):
        assert not diagnostics.record_delivery(live, key)
    else:
        with pytest.raises(ValueError):
            diagnostics.record_delivery(live, key)
    assert diagnostics.pending(live)[0]['notice_key'] == key


def test_receipt_dedup_and_new_evidence_never_reuses_delivered_key(live, clock, monkeypatch):
    receiver, row = due(live, clock)
    key = row['notice_key']
    read = bind(live, key)
    sent = readback(read)
    reads = []
    monkeypatch.setattr(notices, '_sent', lambda ident: reads.append(ident) or sent)
    assert diagnostics.record_delivery(live, key)
    assert not diagnostics.record_delivery(live, key)
    assert reads == [read['id']] and diagnostics.pending(live) == []
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    live.close()
    conn = kbc.connect(path)
    try:
        clock[0] = 'process-reopened'
        helpers.helpers.poll(conn, receiver)
        assert diagnostics.pending(conn) == []
        assert not diagnostics.record_delivery(conn, key)
        receiver.entries = [helpers.helpers.entry(digest='b' * 64)]
        helpers.helpers.poll(conn, receiver)
        for _ in range(5):
            clock[1] += 60000
            helpers.helpers.poll(conn, receiver)
        current = diagnostics.pending(conn)
        assert len(current) == 1 and current[0]['notice_key'] != key
        assert current[0]['evidence_sha256'] == 'b' * 64 and current[0]['receipt_json'] is None
        assert not diagnostics.record_delivery(conn, key)
        assert conn.execute('SELECT COUNT(*) FROM kanban_replay_diagnostic1').fetchone()[0] == 1
    finally:
        conn.close()


def test_stale_receipt_cannot_mark_successor_or_retarget_owner(live, clock, monkeypatch):
    receiver, row = due(live, clock)
    key = row['notice_key']
    read = bind(live, key)
    with pytest.raises(ValueError, match='binding changed'):
        diagnostics.bind_owner(live, key, gateway='other-gw', profile='default', chat='other-chat')
    before = deepcopy(diagnostics.pending(live))

    def stale(ident):
        receiver.entries = []
        helpers.helpers.poll(live, receiver)
        return readback(read)

    monkeypatch.setattr(notices, '_sent', stale)
    assert not diagnostics.record_delivery(live, key)
    assert before[0]['notice_key'] == key and diagnostics.pending(live) == []
    assert live.execute('SELECT COUNT(*) FROM kanban_replay_diagnostic1').fetchone()[0] == 0
