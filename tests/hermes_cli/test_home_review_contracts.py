"""STOP normalization, transferred probe clocks and public refusal contracts. [daveey]"""
from contextlib import nullcontext
import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as dbc, kanban_sync_client as client
from hermes_cli import kanban_operator_labels as labels, kanban_home_transfer as transfer
from hermes_cli import kanban_home_probe as probe, kanban_wake_check as wake
from hermes_cli.kanban_scope_renewal import RenewalRefused
from tests.hermes_cli import test_kanban_home_source_stop as stop
from tests.hermes_cli import test_home_probe_custody as probes
from tests.hermes_cli import test_kanban_scope_renewal_synced as rs
from tests.hermes_cli.test_home_typed_capture import mirror

board = stop.board
live = rs.live


def test_stop_accepts_wire_millisecond_timestamp(board):
    task, record = stop.prepared(board)
    fields = record['rows'][0]['fields']
    original = fields['created_at']
    fields['created_at'] *= 1000
    with dbc.write_txn(board):
        result = transfer.finalize_source(board, record)
    assert result['phase'] == 'stopped_local'
    assert board.execute('SELECT created_at FROM tasks WHERE id=?', (task,)).fetchone()[0] == original


def test_stop_rejects_real_timestamp_change_and_rolls_back(board):
    task, record = stop.prepared(board)
    record['rows'][0]['fields']['created_at'] = (record['rows'][0]['fields']['created_at'] + 1) * 1000
    with dbc.write_txn(board), pytest.raises(ValueError, match='changed task field: created_at'):
        transfer.finalize_source(board, record)
    assert board.execute('SELECT count(*) FROM kanban_home_finalized1').fetchone()[0] == 0
    assert board.execute('SELECT created_on FROM tasks WHERE id=?', (task,)).fetchone()[0] == 'worker-a3'


def moved_park(board, *, explicit=None):
    support = probes.support
    support.prepare_destination(board)
    record = probes.probe_manifest(host=explicit)
    support.materialize(board, record)
    support.complete_feed(board, record)
    return record['task']


def test_old_local_park_does_not_become_silent_at_home_transition(board):
    task = moved_park(board)
    now = 1000 + wake.ALIVE_EVERY + 240 + wake.SILENT_GRACE + 5
    assert wake.restore_missing_checks(board, now=now)['woken'] == []
    assert board.execute('SELECT status FROM tasks WHERE id=?', (task,)).fetchone()[0] == 'scheduled'
    assert wake.get_state(board, task) is None  # never execute on the destination


def test_transfer_liveness_grace_is_bounded_and_survives_reopen(board):
    task = moved_park(board)
    start = 1000 + wake.ALIVE_EVERY + 240 + wake.SILENT_GRACE + 5
    limit = wake.ALIVE_EVERY + 240 + wake.SILENT_GRACE
    assert wake.restore_missing_checks(board, now=start)['woken'] == []
    path = board.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(path) as other:
        other.row_factory = sqlite3.Row
        before = other.total_changes
        assert wake.restore_missing_checks(other, now=start + limit)['woken'] == []
        assert other.total_changes == before
        assert wake.restore_missing_checks(other, now=start + limit + 1)['woken'] == [task]
        assert other.execute('SELECT first_at FROM kanban_home_probe_watch1').fetchone()[0] == start
        assert other.execute('SELECT status FROM tasks WHERE id=?', (task,)).fetchone()[0] == 'todo'


def test_post_transfer_alive_extends_observation_not_baseline(board):
    task = moved_park(board)
    limit = wake.ALIVE_EVERY + 240 + wake.SILENT_GRACE
    wake.restore_missing_checks(board, now=9000)
    with dbc.write_txn(board):
        kb._append_event(board, task, wake.ALIVE_EVENT, {})
        board.execute('UPDATE task_events SET created_at=? WHERE task_id=? AND kind=?', (9500, task, wake.ALIVE_EVENT))
    assert wake.restore_missing_checks(board, now=9500 + limit)['woken'] == []
    assert wake.restore_missing_checks(board, now=9501 + limit)['woken'] == [task]
    assert board.execute('SELECT first_at FROM kanban_home_probe_watch1').fetchone()[0] == 9000


def test_explicit_remote_probe_keeps_existing_silence_contract(board):
    task = moved_park(board, explicit='worker-a2')
    assert wake.restore_missing_checks(board, now=1000 + wake.ALIVE_EVERY + 240 + wake.SILENT_GRACE + 1)['woken'] == [task]
    assert not board.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_home_probe_watch1'").fetchone()


def test_new_park_cannot_borrow_historical_transfer_grace(board):
    task = moved_park(board)
    with dbc.write_txn(board):
        kb._append_event(board, task, 'scheduled', {'wake_check': 'new check'})
        event = board.execute('SELECT MAX(id) FROM task_events WHERE task_id=?', (task,)).fetchone()[0]
    assert probe.remote_baseline(board, task, event, 'worker-a3', None, 9000) == 0
    assert not board.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_home_probe_watch1'").fetchone()


def test_pending_transfer_never_installs_liveness_grace(board):
    support = probes.support
    support.prepare_destination(board)
    record = probes.probe_manifest()
    support.materialize(board, record)
    assert wake.restore_missing_checks(board, now=9000) == {'restored': [], 'woken': []}
    assert not board.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_home_probe_watch1'").fetchone()


def test_pending_renewal_uses_public_refusal_contract(live, monkeypatch):
    hub = rs.Hub({'decision_retain': 1, 'scope_renewal': 1})
    tid, episode, comment = rs.stopped(live, monkeypatch, hub)
    mirror(live, tid, 'renewal-capture-epoch', pending=1)
    with pytest.raises(RenewalRefused) as caught:
        rs.renew(live, tid, episode, comment)
    assert caught.value.code == 'home_capture_refused'
    assert not hub.sent


def test_pending_renewal_command_returns_structured_refusal(live, monkeypatch, capsys):
    cli = rs.cli
    hub = rs.Hub({'decision_retain': 1, 'scope_renewal': 1})
    tid, episode, comment = rs.stopped(live, monkeypatch, hub)
    mirror(live, tid, 'renewal-capture-epoch', pending=1)
    original = cli.renew

    def authorized(conn, task_id, **kwargs):
        return original(conn, task_id, **kwargs, writer=rs.AGENT, env={}, ancestry=[{'HERMES_SESSION_ID': rs.CHAT}])

    monkeypatch.setattr(cli, 'renew', authorized)
    monkeypatch.setattr(dbc, 'connect_closing', lambda: nullcontext(live))
    args = SimpleNamespace(task_id=tid, episode=episode, mandate_comment=comment, reason='authorized', json=True)
    assert cli.command(args) == 1
    result = json.loads(capsys.readouterr().out)
    assert result == {'ok': False, 'error': 'home_capture_refused', 'message': 'home transfer pending'}
    assert not hub.sent
    assert not live.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='scope_renewed'", (tid,)).fetchone()


@pytest.mark.parametrize('state,terminal', [('deleted', True), ('absent', False), ('unavailable', False), ('live', False)])
def test_deleted_label_target_terminates_delivery(board, monkeypatch, state, terminal):
    task = stop.support.seed(board)
    event = 'label-deleted-target-0001'
    with dbc.write_txn(board):
        for sql in labels.SCHEMA:
            board.execute(sql)
        board.execute("INSERT INTO operator_label_jobs(request_uid,board,machine,labels,caller,result_task_id,"
                      "state,outcome,detail,event_uid,created_at,delivery_attempts) "
                      "VALUES ('request','default','worker-a3','[]','{}',?,'done','verified','ok',?,1,0)",
                      (task, event))
    sent = []
    hub = SimpleNamespace(row_evidence=lambda *args: {'state': state, 'row': None},
                          ops=lambda *args: sent.append(args), close=lambda: None)
    monkeypatch.setattr(client, 'load_identity', lambda: None)
    monkeypatch.setattr(client, 'Hub', lambda _: hub)
    labels._deliver(board, 'hub', 'default', now=10)
    row = board.execute('SELECT delivered_at,delivery_attempts,last_error FROM operator_label_jobs').fetchone()
    assert (row[0] is not None) is terminal
    assert row[1] == 1
    if terminal:
        assert row[2].startswith('not_found')
    assert sent == []
