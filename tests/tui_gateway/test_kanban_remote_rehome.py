"""Home changes must not grant a second delivery of the same terminal state."""
import json
import sqlite3
import time

import pytest

from tests.tui_gateway import test_kanban_remote_notice_once as once
from tui_gateway import kanban_remote_notices as krn


def test_legacy_mixed_alias_skip_retires_ended_receipt(tmp_path, monkeypatch):
    now = time.time()
    hub = tmp_path / 'replica.db'
    hub.touch()
    ledger = tmp_path / 'ledger.db'
    task = {'id': 't_dup', 'status': 'done', 'completed_at': now}
    state = krn._notice_state(task)
    old, live, legacy = [f'{h}/default/default/t_dup' for h in ('a', 'b', 'c')]
    sid = once.CHAT
    cid = (sid, old, state)
    home = ['c']
    monkeypatch.setattr(krn, '_chat_index', lambda *_a, **_kw: (
        'macbook', {sid: [{'gw': home[0], 'key': 'default/default/t_dup', 'task': task}]}, {}))
    conn = krn._ledger(ledger)
    pid, start = krn._my_holder()
    try:
        with conn:
            for ref in (old, live):
                conn.execute(
                    'INSERT INTO notices(session,ref,state,at,delivered,holder_pid,holder_start,notice_key) '
                    'VALUES(?,?,?,?,0,?,?,?)', (sid, ref, state, now, pid, start, 'key-' + ref))
            conn.execute('INSERT INTO notices(session,ref,state,at,delivered,legacy_seen) VALUES(?,?,?,?,1,0)',
                         (sid, legacy, state, now))
        krn._track_woken(cid, {'running': False})
        args = dict(hub_db=hub, ledger=ledger, now=now, transcript_has=lambda *_: False)
        assert krn.collect(sid, 'default', **args) == []
        assert conn.execute('SELECT 1 FROM notices WHERE ref=?', (old,)).fetchone() is None
        assert krn.release({'session': sid, 'ref': live, 'state': state}, ledger=ledger)
        home[0] = 'a'
        first = krn.collect(sid, 'default', **args)
        second = krn.collect(sid, 'default', **args)
        assert [item['ref'] for item in first] == [old]
        assert second == [], 'the first new buffer still owns custody'
    finally:
        krn._forget_woken(cid)
        conn.close()


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


@pytest.mark.parametrize('ages, expected', [([None, 20], None), ([20, 10], 10), ([10, 20], 10)])
def test_moved_observation_inherits_unknown_or_earliest_alias(tmp_path, ages, expected):
    from tui_gateway import kanban_remote_observations as observations
    ledger = tmp_path / 'ledger.db'
    task = {'id': 't_dup', 'status': 'done', 'completed_at': 100}
    key = 'default/default/t_dup'
    observations._persist('source', [('worker-6', key, task)], ledger, 100)
    with sqlite3.connect(ledger) as conn:
        conn.execute('UPDATE observations SET first_seen=?', (ages[0],))
        conn.execute('INSERT INTO observations VALUES (?,?,?,?)',
                     ('source', f'worker-7/{key}', krn._notice_state(task), ages[1]))
    observations._persist('source', [('hub', key, task)], ledger, 110)
    with sqlite3.connect(ledger) as conn:
        assert conn.execute('SELECT first_seen FROM observations WHERE ref=?', (f'hub/{key}',)).fetchone() == (expected,)


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


def test_passive_claim_contention_never_falls_back_to_unclaimed_write(tmp_path, monkeypatch):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    def busy(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(krn, '_claim_notice', busy)
    with once._chat(tmp_path) as (db, opener):
        assert krn.sweep_remote_notices(replica_path=hub, gateway='macbook', session_db=opener,
                                        ledger=ledger, now=now, waker=None) == []
        assert db.get_messages(once.CHAT) == []


@pytest.mark.parametrize('lines, expected', [
    (['context', 'Open task: @task:default@gw/t_one', 'keep'],
     ['context', 'Open task: @task:default@gw/t_one', 'keep']),
    (['Open task: @task:default@gw/t_one', 'Next action for the user: obsolete', 'keep'],
     ['Open task: @task:default@gw/t_one', 'keep']),
    (['Next action for the user: obsolete', 'Open task: @task:default@gw/t_one',
      'keep', 'Open task: @task:default@gw/xt_one'], ['keep', 'Open task: @task:default@gw/xt_one']),
    (['Next action for the user: obsolete', 'Open task: @task:default@gw/t_one   ',
      'keep', 'Open task: @task:default@gw/t_other'], ['keep', 'Open task: @task:default@gw/t_other']),
])
def test_ask_removal_preserves_non_ask_links(lines, expected):
    from gateway import kanban_wake_fold as fold
    assert fold._without_superseded_ask(lines, 't_one') == expected


def test_guidance_does_not_strip_present_or_unrecognised_card_tail():
    from gateway import kanban_wake_fold as fold
    text = '[kanban] Task t_one blocked.\nNext action for the user: answer\nOpen task: @task:default@gw/t_one'
    cards = [{'task_id': 't_one', 'text': text}]
    assert fold._guidance(text + '\n\nguidance', cards) == 'guidance'
    cards[0]['text'] = 'Remote blocked\nNext action for the user: answer\nOpen task: @task:default@gw/t_one\ncontext'
    assert fold._guidance('unrelated\ncontext\n\nguidance', cards) is None


@pytest.mark.parametrize('error_type', [OSError, sqlite3.OperationalError])
def test_passive_ledger_open_failure_still_writes_notice(tmp_path, monkeypatch, error_type):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    # Populate observation/memo setup before failing only the delivery ledger open.
    krn._load_swept(ledger)
    with once._chat(tmp_path) as (db, opener):
        original = krn._ledger
        opens = []
        def unavailable(path=None):
            opens.append(path)
            if len(opens) == 1:
                raise error_type('unavailable ledger')
            return original(path)
        monkeypatch.setattr(krn, '_ledger', unavailable)
        result = krn.sweep_remote_notices(replica_path=hub, gateway='macbook', session_db=opener,
                                        ledger=ledger, now=now, waker=None)
        assert len(result) == 1
        assert len(db.get_messages(once.CHAT)) == 1
        monkeypatch.setattr(krn, '_ledger', original)


def test_successful_passive_append_retains_custody_until_ack(tmp_path, monkeypatch):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    original_ack = krn.ack
    checked = []

    def ack_with_custody(claim, *, ledger=None):
        with sqlite3.connect(ledger) as conn:
            row = conn.execute('SELECT delivered, holder_pid FROM notices WHERE session=? AND ref=? AND state=?',
                               (claim['session'], claim['ref'], claim['state'])).fetchone()
        checked.append(row)
        assert row is not None and row[0] == 0 and row[1] is not None
        return original_ack(claim, ledger=ledger)

    monkeypatch.setattr(krn, 'ack', ack_with_custody)
    with once._chat(tmp_path) as (db, opener):
        result = krn.sweep_remote_notices(replica_path=hub, gateway='macbook', session_db=opener,
                                        ledger=ledger, now=now, waker=None)
        assert len(result) == 1
        assert len(db.get_messages(once.CHAT)) == 1
    assert len(checked) == 1 and checked[0] is not None


def test_passive_transcript_ack_keeps_receipt_without_rewriting(tmp_path, monkeypatch):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    _move(hub, 'worker-6')
    with sqlite3.connect(hub) as conn:
        task = json.loads(conn.execute('SELECT data FROM replica').fetchone()[0])
    key, metadata, text = krn._sweep_payload('worker-6', 'default/default/t_dup', task,
                                           'macbook', 'default', once.CHAT)
    with once._chat(tmp_path) as (db, opener):
        once._turn_row(db, once.CHAT, text, metadata)
        def fail(*args, **kwargs):
            raise OSError('already present; unnecessary append failed')
        monkeypatch.setattr(db, 'append_kanban_notice', fail)
        krn.sweep_remote_notices(replica_path=hub, gateway='macbook', session_db=opener,
                                 ledger=ledger, now=now, waker=None)
        with sqlite3.connect(ledger) as conn:
            assert conn.execute('SELECT delivered, notice_key FROM notices').fetchall() == [(krn.DELIVERED, key)]
        _move(hub, 'hub')
        assert krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 1,
                           transcript_has=lambda k, _: k == key) == []
        assert len(db.get_messages(once.CHAT)) == 1


def test_passive_failed_write_releases_claim_for_retry(tmp_path, monkeypatch):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    with once._chat(tmp_path) as (db, opener):
        original = db.append_kanban_notice
        def fail(*args, **kwargs):
            raise OSError('temporary write failure')
        monkeypatch.setattr(db, 'append_kanban_notice', fail)
        assert krn.sweep_remote_notices(replica_path=hub, gateway='macbook', session_db=opener,
                                        ledger=ledger, now=now, waker=None) == []
        monkeypatch.setattr(db, 'append_kanban_notice', original)
        krn.sweep_remote_notices(replica_path=hub, gateway='macbook', session_db=opener,
                                 ledger=ledger, now=now + 1, waker=None)
        assert len(db.get_messages(once.CHAT)) == 1


def test_passive_sweep_reserves_custody_before_writing(tmp_path, monkeypatch):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    with once._chat(tmp_path) as (db, opener):
        original = db.append_kanban_notice
        competing = []
        def append(*args, **kwargs):
            _move(hub, 'other-home')
            competing.extend(krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 1))
            return original(*args, **kwargs)
        monkeypatch.setattr(db, 'append_kanban_notice', append)
        krn.sweep_remote_notices(replica_path=hub, gateway='macbook', session_db=opener,
                                 ledger=ledger, now=now, waker=None)
        assert len(db.get_messages(once.CHAT)) == 1
        assert competing == []


def test_opposite_home_claims_are_serialized(tmp_path):
    import concurrent.futures
    import threading

    ledger = tmp_path / 'ledger.db'
    krn._ledger(ledger).close()
    start = threading.Barrier(2)
    scanned = threading.Barrier(2)

    def claim(home):
        conn = krn._ledger(ledger)
        def transcript_has(*_):
            try:
                scanned.wait(timeout=0.3)
            except threading.BrokenBarrierError:
                pass  # a serialized claimant cannot reach this until the other commits
            return False
        try:
            start.wait(timeout=3)
            return krn._claim_notice(conn, session=once.CHAT, ref=f'{home}/default/default/t_dup',
                                     state='done@1', now=10, fresh=True, notice_key=f'remote:{home}:event',
                                     task_id='t_dup', transcript_has=transcript_has)
        finally:
            conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        verdicts = list(executor.map(claim, ['worker-6', 'hub']))
    assert verdicts.count('offer') == 1
    assert verdicts.count('skip') == 1


def test_unconfirmed_legacy_delivery_does_not_suppress_moved_state(tmp_path):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    _move(hub, 'worker-6')
    krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now)
    with sqlite3.connect(ledger) as conn:
        conn.execute('UPDATE notices SET delivered=1, notice_key=NULL, legacy_seen=0')
    _move(hub, 'hub')
    offered = krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 10,
                          transcript_has=lambda *_: False)
    assert len(offered) == 1


def test_legacy_home_recovers_after_moved_successor_fails(tmp_path):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    _move(hub, 'worker-6')
    krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now)
    with sqlite3.connect(ledger) as conn:
        conn.execute('UPDATE notices SET delivered=1, notice_key=NULL, legacy_seen=0')
    _move(hub, 'hub')
    [new] = krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 10,
                        transcript_has=lambda *_: False)
    krn.release(new, ledger=ledger)
    _move(hub, 'worker-6')
    offered = krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 20,
                          transcript_has=lambda *_: False)
    assert len(offered) == 1


def test_opposite_home_legacy_reoffers_are_serialized(tmp_path, monkeypatch):
    import concurrent.futures
    import threading
    from tui_gateway import kanban_remote_identity as identity
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    with sqlite3.connect(hub) as db:
        task = json.loads(db.execute('SELECT data FROM replica').fetchone()[0])
    conn = krn._ledger(ledger)
    state = krn._notice_state(task)
    with conn:
        for home in ['worker-6', 'hub']:
            conn.execute('INSERT INTO notices (session, ref, state, at, delivered, legacy_seen) VALUES (?,?,?,?,1,0)',
                         (once.CHAT, f'{home}/default/default/t_dup', state, now))
    conn.close()
    scanned = threading.Barrier(2)
    start = threading.Barrier(2)
    original = identity.moved_claim
    def fenced(*args, **kwargs):
        verdict = original(*args, **kwargs)
        try:
            scanned.wait(timeout=0.3)
        except threading.BrokenBarrierError:
            pass
        return verdict
    monkeypatch.setattr(identity, 'moved_claim', fenced)
    def claim(home):
        db = krn._ledger(ledger)
        try:
            start.wait(timeout=3)
            return krn._reconcile_legacy(db, once.CHAT,
                                         [{'gw': home, 'key': 'default/default/t_dup', 'task': task}],
                                         now, lambda *_: False, 'macbook', 'default')
        finally:
            db.close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        offers = list(executor.map(claim, ['worker-6', 'hub']))
    assert sorted(map(len, offers)) == [0, 1]


@pytest.mark.parametrize('legacy', [False, True])
def test_collect_callback_failure_releases_earlier_unreturned_claim(tmp_path, monkeypatch, legacy):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    tasks = [{'id': tid, 'status': 'done', 'completed_at': now} for tid in ['t_first', 't_second']]
    cards = [{'gw': 'worker-6', 'key': f"default/default/{task['id']}", 'task': task} for task in tasks]
    monkeypatch.setattr(krn, '_chat_index', lambda *_a, **_kw: ('macbook', {once.CHAT: cards}, {}))
    if legacy:
        conn = krn._ledger(ledger)
        with conn:
            for card in cards:
                conn.execute('INSERT INTO notices (session, ref, state, at, delivered, legacy_seen) '
                             'VALUES (?,?,?,?,1,0)',
                             (once.CHAT, f"worker-6/{card['key']}", krn._notice_state(card['task']), now))
        conn.close()
    calls = []
    def fails_second(key, task_id):
        calls.append((key, task_id))
        if 't_second' in key:
            raise OSError('second transcript unavailable')
        return False
    assert krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now,
                       transcript_has=fails_second) == []
    assert any('t_second' in key for key, _ in calls)
    # Same holder must immediately retry, not wait for its process to die.
    offered = krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 1,
                           transcript_has=lambda *_: False)
    assert {item['ref'].rsplit('/', 1)[-1] for item in offered} == {'t_first', 't_second'}


def test_legacy_reconciliation_preserves_replaced_keyed_receipt(tmp_path):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    [old] = krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now)
    with sqlite3.connect(ledger) as conn:
        conn.execute('UPDATE notices SET delivered=1, notice_key=NULL, legacy_seen=0')
    replaced = False
    def transcript_has(*_):
        nonlocal replaced
        if not replaced:
            replaced = True
            assert krn.release(old, ledger=ledger)
            fresh = krn._ledger(ledger)
            try:
                assert krn._claim_notice(fresh, session=old['session'], ref=old['ref'], state=old['state'],
                                         now=now, fresh=True, notice_key=old['event_key'], task_id='t_dup',
                                         transcript_has=None) == 'offer'
            finally:
                fresh.close()
            assert krn.ack(old, ledger=ledger)
        return False
    assert krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 1,
                       transcript_has=transcript_has) == []
    assert replaced
    with sqlite3.connect(ledger) as conn:
        assert conn.execute('SELECT delivered, notice_key FROM notices').fetchall() == [(krn.DELIVERED, old['event_key'])]


def test_legacy_returning_home_respects_live_successor(tmp_path):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    _move(hub, 'worker-6')
    krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now)
    with sqlite3.connect(ledger) as conn:
        conn.execute('UPDATE notices SET delivered=1, notice_key=NULL, legacy_seen=0')
    _move(hub, 'hub')
    with once._chat(tmp_path) as (_db, opener):
        waker = once._Waker('started')
        krn.sweep_remote_notices(replica_path=hub, gateway='macbook', session_db=opener,
                                 ledger=ledger, now=now + 10, waker=waker)
        assert waker.calls == 1
        _move(hub, 'worker-6')
        assert krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 20,
                           transcript_has=lambda *_: False) == []


@pytest.mark.parametrize('running', [False, True])
def test_moved_woken_claim_recovers_only_after_turn_ends(tmp_path, running):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    _move(hub, 'worker-6')
    [old] = krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now)
    _move(hub, 'hub')
    claim_id = (old['session'], old['ref'], old['state'])
    krn._track_woken(claim_id, {'running': running})
    try:
        offered = krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 10,
                              transcript_has=lambda *_: False)
        assert len(offered) == (0 if running else 1)
        if not running:
            assert krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 11,
                               transcript_has=lambda *_: False) == []
    finally:
        krn._forget_woken(claim_id)


def test_ended_alias_survives_successor_transaction_rollback(tmp_path):
    ledger = tmp_path / 'ledger.db'
    old_ref = 'worker-6/default/default/t_dup'
    new_ref = 'hub/default/default/t_dup'
    args = dict(session=once.CHAT, state='done@1', now=10, fresh=True, task_id='t_dup')
    conn = krn._ledger(ledger)
    claim_id = (once.CHAT, old_ref, 'done@1')
    try:
        assert krn._claim_notice(conn, ref=old_ref, notice_key='old-key',
                                 transcript_has=lambda *_: False, **args) == 'offer'
        krn._track_woken(claim_id, {'running': False})
        def interrupted(key, _task):
            if key == 'new-key':
                raise OSError('successor reservation interrupted')
            return False
        with pytest.raises(OSError, match='successor reservation interrupted'):
            krn._claim_notice(conn, ref=new_ref, notice_key='new-key', transcript_has=interrupted, **args)
        assert conn.execute('SELECT ref FROM notices').fetchone()['ref'] == old_ref
        assert krn._claim_notice(conn, ref=new_ref, notice_key='new-key',
                                 transcript_has=lambda *_: False, **args) == 'offer'
    finally:
        conn.close()
        krn._forget_woken(claim_id)


def test_return_home_after_ended_alias_and_failed_successor_retries(tmp_path):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    _move(hub, 'worker-6')
    [old] = krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now)
    claim_id = (old['session'], old['ref'], old['state'])
    _move(hub, 'hub')
    krn._track_woken(claim_id, {'running': False})
    try:
        [new] = krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 10,
                            transcript_has=lambda *_: False)
        krn.release(new, ledger=ledger)
        _move(hub, 'worker-6')
        offered = krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 20,
                              transcript_has=lambda *_: False)
        assert len(offered) == 1
        assert offered[0]['ref'] == old['ref']
    finally:
        krn._forget_woken(claim_id)


@pytest.mark.parametrize('delivered', [False, True])
def test_returning_old_home_respects_successor_custody(tmp_path, monkeypatch, delivered):
    now = time.time()
    hub = once._hub(tmp_path, now)
    ledger = tmp_path / 'ledger.db'
    _move(hub, 'worker-6')
    [old] = krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now)
    with sqlite3.connect(ledger) as conn:
        conn.execute('UPDATE notices SET holder_pid=987654, holder_start=0')
    monkeypatch.setattr(krn, '_holder_alive', lambda pid, start: pid != 987654)
    _move(hub, 'hub')
    [new] = krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 10)
    if delivered:
        assert krn.ack(new, ledger=ledger)
    _move(hub, 'worker-6')
    assert krn.collect(once.CHAT, 'default', hub_db=hub, ledger=ledger, now=now + 20) == []
    if delivered:
        with sqlite3.connect(ledger) as conn:
            assert conn.execute('SELECT settled FROM notices WHERE ref=?', (old['ref'],)).fetchone() == ('moved',)


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
