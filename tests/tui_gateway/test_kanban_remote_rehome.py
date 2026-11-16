"""Home changes must not grant a second delivery of the same terminal state."""
import json
import sqlite3
import time

import pytest

from tests.tui_gateway import test_kanban_remote_notice_once as once
from tui_gateway import kanban_remote_notices as krn


def _move(path, gateway):
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE replica SET gw=?, seq=seq+1', (gateway,))
    krn._reset_for_tests()


@pytest.mark.parametrize('door', ['collect', 'sweep', 'passive'])
def test_delivered_state_is_settled_moved_without_another_turn(tmp_path, door):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    _move(hub, 'worker-6')
    with once._chat(tmp_path) as (db, opener):
        krn.sweep_remote_notices(replica_path=hub, gateway='macbook', session_db=opener,
                                 ledger=ledger, now=now, waker=once._Waker('missing'))
        assert len(db.get_messages(once.CHAT)) == 1
        _move(hub, 'hub')
        waker = once._Waker('started')
        if door == 'collect':
            assert krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 10) == []
        else:
            assert krn.sweep_remote_notices(replica_path=hub, gateway='macbook', session_db=opener,
                                            ledger=ledger, now=now + 10, waker=None if door == 'passive' else waker) == []
        assert waker.calls == 0
        assert len(db.get_messages(once.CHAT)) == 1
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT delivered, settled FROM notices WHERE ref='hub/default/default/t_dup'").fetchone() == (krn.SETTLED, 'moved')


def test_moved_observation_keeps_earliest_late_window(tmp_path, caplog):
    from tests.tui_gateway import test_kanban_remote_late_observed as late
    ledger = tmp_path / 'ledger.db'
    hub = late._observed_then_starved(tmp_path, ledger)
    _move(hub, 'worker-6')
    now = late.T0 + 120 + 88 * 3600
    assert krn.collect(late.CHAT, 'default', hub_db=hub, ledger=ledger, now=now) == []
    assert 'age 88h00m' in caplog.text and 'dropped undelivered notice' in caplog.text
    with sqlite3.connect(ledger) as conn:
        row = conn.execute('SELECT delivered, settled FROM notices WHERE ref=?',
                           (f'worker-6/default/default/{late.TID}',)).fetchone()
        assert row == (krn.SETTLED, 'expired')


def test_dead_holder_old_home_transcript_key_is_not_reoffered(tmp_path, monkeypatch):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    _move(hub, 'worker-6')
    [old] = krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now)
    monkeypatch.setattr(krn, '_holder_alive', lambda *_: False)
    _move(hub, 'hub')
    seen = []

    def transcript_has(key, task_id):
        assert not task_id
        seen.append(key)
        return key == old['event_key']

    assert krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 10,
                       transcript_has=transcript_has) == []
    assert old['event_key'] in seen


def test_delivered_moved_old_state_does_not_log_undelivered(tmp_path, caplog):
    from tests.tui_gateway import test_kanban_remote_late_observed as late
    ledger = tmp_path / 'ledger.db'
    hub = late._observed_then_starved(tmp_path, ledger)
    [old] = krn.collect(late.CHAT, 'default', hub_db=hub, ledger=ledger, now=late.T0 + 3 * 3600)
    assert krn.ack(old, ledger=ledger)
    _move(hub, 'worker-6')
    assert krn.collect(late.CHAT, 'default', hub_db=hub, ledger=ledger, now=late.T0 + 88 * 3600) == []
    assert 'dropped undelivered notice' not in caplog.text


def test_live_holder_keeps_custody_after_home_move(tmp_path):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    _move(hub, 'worker-6')
    [old] = krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now)
    _move(hub, 'hub')
    assert krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 10) == []
    assert krn.ack(old, ledger=ledger)
    assert krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 20) == []


def test_new_terminal_state_after_move_still_delivers_once(tmp_path):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    _move(hub, 'worker-6')
    [old] = krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now)
    assert krn.ack(old, ledger=ledger)
    with sqlite3.connect(hub) as conn:
        task = json.loads(conn.execute('SELECT data FROM replica').fetchone()[0])
        task['completed_at'] = now + 10
        task['completion_notice']['event_id'] += 1
        conn.execute('UPDATE replica SET gw=?, data=?, seq=seq+1', ('hub', json.dumps(task)))
    krn._reset_for_tests()
    [new] = krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 20)
    assert new['state'] != old['state']
    assert krn.ack(new, ledger=ledger)
    assert krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 21) == []
