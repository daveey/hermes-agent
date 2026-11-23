"""f-only loaded notification acceptance, with real replica and claim ledgers."""
import time

import pytest

from tests.tui_gateway import test_kanban_remote_notices_keyed as fixtures
from tui_gateway import kanban_remote_notices as krn


def _hub(tmp_path, cards):
    import sqlite3
    path = fixtures._hub(tmp_path, cards)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE INDEX replica_poll_metadata ON replica(coll, seq)')
    return path


def test_remote_delivered_by_shared_reconcile_with_pollers_off(tmp_path, monkeypatch):
    from tui_gateway import notification_loaded_f as loaded
    from tui_gateway import server
    from hermes_cli import fact_host, fact_replica, fact_demand

    def no_ipc(*args, **kwargs):
        pytest.fail('first-ship f must not start replica IPC or a demand channel')

    monkeypatch.setattr(fact_host.FactHost, 'start', no_ipc)
    monkeypatch.setattr(fact_replica.ReplicaHandle, '__init__', no_ipc)
    monkeypatch.setattr(fact_demand.DemandChannel, '__init__', no_ipc)
    old = fixtures._card('t_history', fixtures.CHAT, status='done', at=time.time() - krn.FRESH_S - 60)
    alias = fixtures._card('t_history', fixtures.CHAT, status='done', at=time.time())
    alias['key'] = 'default/t_history'
    replica = _hub(tmp_path, [old, alias])
    ledger = tmp_path / 'ledger.db'
    monkeypatch.setattr(krn, 'hub_db_path', lambda: replica)
    monkeypatch.setattr(krn, 'ledger_path', lambda: ledger)
    monkeypatch.setattr(server, '_session_lineage', lambda *_: ())
    monkeypatch.setattr(server, '_forwarded_locally', lambda *_: False)
    monkeypatch.setattr(server, '_remote_transcript_has', lambda *_: lambda *_: False)
    clock = [100.0]
    scheduler = loaded.Scheduler(clock=lambda: clock[0])
    session = {'session_key': fixtures.CHAT, 'profile_home': None}
    action = scheduler.add(session, 'default', lambda: None)
    from hermes_cli import kanban_db
    import sqlite3
    local_board = tmp_path / 'local.db'
    with sqlite3.connect(local_board) as conn:
        conn.execute('CREATE TABLE kanban_notify_subs (platform TEXT, chat_id TEXT)')
        conn.executemany('INSERT INTO kanban_notify_subs VALUES (?, ?)', [('tui', fixtures.CHAT), ('tui', 'foreign')])
    inventories = []
    def boards(**_):
        inventories.append(clock[0])
        return [{'slug': 'default', 'db_path': str(local_board)}]
    monkeypatch.setattr(kanban_db, 'list_boards', boards)
    monkeypatch.setattr(server, '_kb_stray_delegate_keys', lambda *_: ())
    scheduler.step()
    assert action.due(legacy_due=False)
    assert session['_notif_local_boards'] == ('default',)
    assert inventories == [100.0]
    assert server._collect_remote_kanban_notifications(fixtures.CHAT, [], session=session) == []
    card = fixtures._card('t_arrived', fixtures.CHAT, status='done', at=time.time())
    fixtures._arrive(replica, card)
    legacy = fixtures._card('t_legacy', fixtures.CHAT, status='done', at=time.time())
    legacy['key'] = 'default/t_legacy'
    fixtures._arrive(replica, legacy, seq=51)
    clock[0] += 29
    scheduler.step()
    assert not action.due(legacy_due=True)  # old per-chat polling is disabled
    clock[0] += 1
    scheduler.step()
    assert action.due(legacy_due=False)
    claims = []
    texts = server._collect_remote_kanban_notifications(fixtures.CHAT, claims, session=session)
    assert len(texts) == len(claims) == 2
    # Exercise the real transcript commit and durable ACK boundary, not a
    # collector-only success. Only the model-turn admission is replaced.
    import threading
    from hermes_state import SessionDB
    state = tmp_path / 'notice-state.db'
    with SessionDB(state) as db:
        db.create_session(fixtures.CHAT, 'desktop')
    session.update(history_lock=threading.Lock(), _kanban_pending=texts, _kanban_claims=claims)
    monkeypatch.setattr(server, '_session_db', lambda *_: SessionDB(state))
    monkeypatch.setattr(server, '_submit_row_target_key', lambda *_: fixtures.CHAT)
    monkeypatch.setattr(server, '_kb_gateway_owns', lambda *_: False)
    monkeypatch.setattr(server, '_kb_settle_held', lambda *_: None)
    monkeypatch.setattr(server, '_kb_owner_refusal', lambda *_: None)
    monkeypatch.setattr(server, '_notif_claim_turn', lambda *_: True)
    monkeypatch.setattr(server, '_notif_release_turn', lambda *_: None)
    monkeypatch.setattr(server, '_emit', lambda *_: None)
    submitted = []
    monkeypatch.setattr(server, '_notif_submit', lambda *args, **kw: submitted.append(kw) or True)
    server._notif_poll_kanban_scoped('ui', session, (texts, claims))
    assert len(submitted) == 1 and submitted[0]['display_kind'] == 'kanban_notice'
    with SessionDB(state) as db:
        assert len(db.get_messages(fixtures.CHAT)) == 1
        assert db.kanban_notice_keys_delivered(fixtures.CHAT, server._kb_text_keys(texts)) == set(server._kb_text_keys(texts))
    assert all(c.get('acked') for c in claims)
    # An incomplete keyed pass retains its retry even with no display rows.
    import sqlite3
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE meta SET v='99' WHERE k='cursor'")
    scheduler.invalidate([('hub', card['key'])])
    scheduler.step()
    assert action.due(legacy_due=False)
    assert server._collect_remote_kanban_notifications(fixtures.CHAT, [], session=session) == []
    retry_at = session['_kanban_remote_retry_at']
    action.tick('ui', retry_at, retry_at - 1, None, lambda *_: None)
    assert scheduler.dirty == {('hub', card['key'])}  # shared keyed retry, not another census
    action.close()


@pytest.mark.parametrize('linked', [False, True])
def test_incomplete_reconciliation_retries_only_positive_chat_keys(tmp_path, monkeypatch, linked):
    import sqlite3
    from tui_gateway import notification_loaded_f as loaded, server
    from tui_gateway import kanban_remote_notices_keyed as keyed, notification_loaded_local

    cards = [fixtures._card(f't_foreign_{i}', 'unrelated-chat', status='done', at=time.time())
             for i in range(1200)]
    relevant = fixtures._card('t_relevant', 'ancestor-chat', status='done', at=time.time())
    if linked:
        cards.append(relevant)
    # A legacy replica has no incremental metadata index: unknown recovery
    # must stay on reconciliation, independently of native key hints.
    replica = fixtures._hub(tmp_path, cards)
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE meta SET v='99' WHERE k='cursor'")
    monkeypatch.setattr(krn, 'hub_db_path', lambda: replica)
    monkeypatch.setattr(krn, 'ledger_path', lambda: tmp_path / 'ledger.db')
    monkeypatch.setattr(server, '_session_lineage', lambda *_: ('ancestor-chat',))
    monkeypatch.setattr(server, '_forwarded_locally', lambda *_: False)
    monkeypatch.setattr(server, '_remote_transcript_has', lambda *_: lambda *_: False)
    monkeypatch.setattr(notification_loaded_local, 'capture', lambda *_: ({}, {}))
    clock = [100.0]
    scheduler = loaded.Scheduler(clock=lambda: clock[0])
    session = {'session_key': fixtures.CHAT, 'profile_home': None}
    action = scheduler.add(session, 'default', lambda: None)
    reads = []
    read_rows = keyed.read_rows

    def read(path, keys):
        reads.append(keys)
        return read_rows(path, keys)

    monkeypatch.setattr(keyed, 'read_rows', read)
    try:
        scheduler.step()
        assert action.due(legacy_due=False)
        texts = server._collect_remote_kanban_notifications(fixtures.CHAT, [], session=session)
        assert len(texts) == int(linked)
        retry_at = session['_kanban_remote_retry_at']
        action.tick('ui', retry_at, retry_at - 1, None, lambda *_: None)
        expected = {('hub', relevant['key'])} if linked else set()
        assert scheduler.dirty == expected
        clock[0] = 101
        scheduler.step()
        assert reads == ([None, list(expected)] if linked else [None])
        clock[0] = 129.999
        scheduler.step()
        assert sum(keys is None for keys in reads) == 1
        clock[0] = 130
        scheduler.step()
        assert sum(keys is None for keys in reads) == 2
    finally:
        action.close()


def test_wake_reaches_loaded_executor_under_two_seconds(tmp_path, monkeypatch):
    import threading
    from tools import async_delegation
    from tools.process_registry import process_registry
    from tui_gateway import notification_loaded_executor as executor
    from tui_gateway import notification_loaded_f as loaded, server, notification_park, producer_admission
    old = fixtures._card('t_history', fixtures.CHAT, status='done', at=time.time() - krn.FRESH_S - 60)
    alias = fixtures._card('t_history', fixtures.CHAT, status='done', at=time.time())
    alias['key'] = 'default/t_history'
    replica = _hub(tmp_path, [old, alias])
    ledger = tmp_path / 'ledger.db'
    monkeypatch.setattr(krn, 'hub_db_path', lambda: replica)
    monkeypatch.setattr(krn, 'ledger_path', lambda: ledger)
    monkeypatch.setattr(server, '_session_lineage', lambda *_: ())
    monkeypatch.setattr(server, '_forwarded_locally', lambda *_: False)
    monkeypatch.setattr(server, '_remote_transcript_has', lambda *_: lambda *_: False)
    monkeypatch.setattr(async_delegation, 'maybe_sweep_orphaned_completions', lambda *_: None)
    monkeypatch.setattr(notification_park, 'has_parked', lambda: False)
    monkeypatch.setattr(producer_admission, 'deliver_pending', lambda *_: None)
    for name in ('_maybe_fire_tui_loop_tick', '_maybe_fire_tui_heartbeat_tick', '_notif_take_parked'):
        monkeypatch.setattr(server, name, lambda *_: None)
    session = {'session_key': fixtures.CHAT, 'profile_home': None}
    other = {'session_key': 'other-chat', 'profile_home': None}
    delivered, initial, completion = threading.Event(), threading.Event(), threading.Event()
    scheduler = loaded.Scheduler()
    monkeypatch.setattr(loaded, '_owner', scheduler)
    monkeypatch.setattr(loaded, 'enabled', lambda: True)
    def poll(_sid, sess, **kwargs):
        claims = []
        texts = server._collect_remote_kanban_notifications(sess['session_key'], claims, session=sess)
        for claim in claims:
            assert krn.ack(claim['remote'])
        if texts and sess is session:
            delivered.set()
        initial.set()
    monkeypatch.setattr(server, '_notif_poll_kanban', poll)
    # Final delivery stays mocked at the existing handle boundary (parent-owned).
    monkeypatch.setattr(server, '_notif_handle_ready',
                        lambda _sid, _sess, events, *_a, **_k: events and completion.set())
    scheduler.start()
    before = {t.ident for t in threading.enumerate()}
    stops = [server._start_notification_poller('ui', session), server._start_notification_poller('ui-2', other)]
    lanes = [lane for stop, lane in list(server._notification_pollers) if stop in stops]
    spawned = []
    try:
        assert len(lanes) == 2 and all(isinstance(lane, executor.Lane) for lane in lanes)
        assert initial.wait(2)
        # f ON: no per-chat OS thread; both chats ride one shared executor thread
        # (a lane is that thread's identity only while it steps).
        shared = executor.executor_for(process_registry.completion_queue).thread
        spawned = [shared]
        assert shared.name == executor.EXECUTOR_THREAD_NAME and shared.is_alive()
        assert {t.ident for t in threading.enumerate()} - before == {shared.ident}
        assert {lane.ident for lane in lanes} == {shared.ident}
        assert not [t for t in threading.enumerate()
                    if t.name.startswith('tui-notif-poller') and not isinstance(t, executor.Lane)]
        process_registry.completion_queue.put({'type': 'completion'})
        assert completion.wait(2)  # completion queue survives the source-poller switch
        card = fixtures._card('t_wake', fixtures.CHAT, status='done', at=time.time())
        fixtures._arrive(replica, card)
        started = time.monotonic()
        scheduler.invalidate([('hub', card['key'])])
        assert delivered.wait(1.9)
        assert time.monotonic() - started < 2
        delivered.clear()
        native = fixtures._card('t_native', fixtures.CHAT, status='done', at=time.time())
        started = time.monotonic()
        fixtures._arrive(replica, native, seq=51)
        assert delivered.wait(1.9)  # real native replica publication, no manual hint
        assert time.monotonic() - started < 2
    finally:
        for stop in stops:
            stop.set()
        for lane in lanes:
            lane.join(3)
        scheduler.close()
    assert not any(lane.is_alive() for lane in lanes)
    for thread in spawned:
        thread.join(3)  # the executor exits once its last lane's cleanup ran
        assert not thread.is_alive()


def test_wake_and_reconcile_share_discovery_without_duplicate_delivery(tmp_path, monkeypatch):
    from tui_gateway import notification_loaded_f as loaded
    from tui_gateway import kanban_remote_notices_keyed as keyed
    replica = _hub(tmp_path, [])
    ledger = tmp_path / 'ledger.db'
    monkeypatch.setattr(krn, 'hub_db_path', lambda: replica)
    monkeypatch.setattr(krn, 'ledger_path', lambda: ledger)
    clock = [100.0]
    scheduler = loaded.Scheduler(clock=lambda: clock[0])
    sessions = [{}, {}]
    actions = [scheduler.add(session, 'default', lambda: None) for session in sessions]
    scheduler.step()
    for action in actions:
        assert action.due(legacy_due=False)
    card = fixtures._card('t_once', fixtures.CHAT, status='done', at=time.time())
    fixtures._arrive(replica, card)
    reads = []
    real_read = keyed.read_rows
    def read(path, keys):
        reads.append(keys)
        return real_read(path, keys)
    monkeypatch.setattr(keyed, 'read_rows', read)
    # Native cues discover changed keys, not a whole payload scan on each wake.
    scheduler.source_changed()
    scheduler.step()
    for action in actions:
        assert action.due(legacy_due=False)
    assert len(reads) == 1 and reads[0] == [('hub', card['key'])]
    assert sessions[0]['_notif_remote_batch'] is sessions[1]['_notif_remote_batch']
    got = krn.collect(fixtures.CHAT, 'default', keys=sessions[0]['_notif_remote_batch'])
    assert len(got) == 1
    assert krn.ack(got[0])
    clock[0] += 30
    scheduler.step()
    for action, session in zip(actions, sessions):
        assert action.due(legacy_due=False)
        assert krn.collect(fixtures.CHAT, 'default', keys=session['_notif_remote_batch']) == []
        action.close()
    assert len(reads) == 2 and reads[1] is None

    # Chat churn and reset/health hints cannot advance the process census clock.
    from types import SimpleNamespace
    monkeypatch.setattr(loaded, '_owner', scheduler)
    monkeypatch.setattr(loaded, 'enabled', lambda: True)
    late_session = {'profile_home': None, 'session_key': fixtures.CHAT}
    late = scheduler.add(late_session, 'default', lambda: None)
    scheduler.step()
    assert late.due(legacy_due=False)  # fresh shared keyed catch-up, not stale evidence
    assert krn.collect(fixtures.CHAT, 'default', keys=late_session['_notif_remote_batch']) == []
    assert sum(keys is None for keys in reads) == 1
    # Cached identities are refreshed, not cached rows replayed: a deletion since
    # the census must not be offered to the next newly loaded chat.
    import sqlite3
    with sqlite3.connect(replica) as conn:
        conn.execute('DELETE FROM replica WHERE gw=? AND key=?', ('hub', card['key']))
    newcomer = scheduler.add({'profile_home': None, 'session_key': fixtures.CHAT}, 'default', lambda: None)
    scheduler.step()
    assert newcomer.due(legacy_due=False)
    assert newcomer.session['_notif_remote_batch'].rows == '[]'
    assert newcomer.session['_notif_remote_batch'] is not late_session['_notif_remote_batch']
    newcomer.close()
    fixtures._arrive(replica, card)
    for reset, health in ((True, False), (False, True)):
        loaded.signal(None, SimpleNamespace(reset=reset, health=health, upserts=(), deletes=()))
        scheduler.step()
        assert sum(keys is None for keys in reads) == 1
    # Overflow discards discovery only; eventual reconciliation repairs it without
    # borrowing a census from the next interval.
    for _ in range(34):
        scheduler.invalidate([('hub', card['key'])])
        scheduler.step()
    assert len(late.pending) <= 32
    assert sum(keys is None for keys in reads) == 1
    clock[0] = 159.999
    scheduler.step()
    assert sum(keys is None for keys in reads) == 1
    clock[0] = 160
    scheduler.step()
    assert sum(keys is None for keys in reads) == 2
    assert late.due(legacy_due=False)

    # Even a failed full capture consumes the interval. Failed keyed work is
    # marked unknown for the next 30s reconciliation, without delaying locals.
    attempts = []
    capture = scheduler._capture
    def fail_capture(profile, keys):
        attempts.append(keys)
        raise RuntimeError('capture unavailable')
    monkeypatch.setattr(scheduler, '_capture', fail_capture)
    clock[0] = 190
    scheduler.step()
    assert attempts == [None]
    clock[0] = 191
    scheduler.invalidate()
    scheduler.step()
    assert attempts == [None]
    scheduler.invalidate([('hub', card['key'])])
    scheduler.step()
    assert attempts[-1] == (('hub', card['key']),)
    assert not scheduler.dirty
    assert scheduler.remote_incomplete
    monkeypatch.setattr(scheduler, '_capture', capture)
    clock[0] = 192
    scheduler.step()
    assert late.due(legacy_due=False)
    assert sum(keys is None for keys in reads) == 2
    clock[0] = 220
    scheduler.step()
    assert sum(keys is None for keys in reads) == 3
    late.close()


def test_off_keeps_legacy_polling_and_collection_parity(tmp_path, monkeypatch):
    import queue
    from types import SimpleNamespace
    from tools import async_delegation, process_registry_awaiting
    from tui_gateway import notification_loaded_f as loaded, server, notification_park, producer_admission
    from tui_gateway.notification_wait import PollerStop
    from hermes_cli import config
    monkeypatch.setattr(config, 'load_config', lambda: {'display': {'loaded_notification_f': False}})
    monkeypatch.setattr(loaded, '_owner', None)
    monkeypatch.setattr(async_delegation, 'maybe_sweep_orphaned_completions', lambda *_: None)
    monkeypatch.setattr(notification_park, 'has_parked', lambda: False)
    monkeypatch.setattr(producer_admission, 'deliver_pending', lambda *_: None)
    for name in ('_maybe_fire_tui_loop_tick', '_maybe_fire_tui_heartbeat_tick', '_notif_take_parked'):
        monkeypatch.setattr(server, name, lambda *_: None)
    clock = [100.0]
    monkeypatch.setattr(server, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    q = queue.Queue()
    stop = PollerStop(q)
    registry = SimpleNamespace(completion_queue=q, has_parked_events=lambda: False)
    calls = []
    monkeypatch.setattr(server, '_notif_poll_kanban', lambda *_: calls.append(clock[0]))
    def take(*_, **kwargs):
        if clock[0] == 100:
            clock[0] += 5
        else:
            stop.set()
        return []
    monkeypatch.setattr(process_registry_awaiting, 'take_ready', take)
    session = {'session_key': fixtures.CHAT, 'profile_home': None}
    server._notification_poller_run(stop, 'ui', session, registry, lambda *_: None)
    assert calls == [100.0, 105.0]
    assert loaded._owner is None and '_notif_remote_batch' not in session
