"""H2/C11: exercise the production host boundary, not delivery return values."""
import contextlib
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from tui_gateway import delegation_unloaded as du
from tui_gateway import kanban_unloaded as ku
from tui_gateway import notice_fanout as nf
from tui_gateway import notice_guarantee as ng
from tui_gateway import reap_notices as rn

REAL_GUARANTEE = ng.guarantee_pass
REAL_SWEEP = ku.sweep_once


@pytest.fixture
def host(monkeypatch):
    live, calls = set(), []
    monkeypatch.setattr(ku, 'live_session_ids', lambda: set(live))
    monkeypatch.setattr(ku, '_board_slugs', lambda _: ['default'])
    monkeypatch.setattr(ku, 'sweep_remote_once', lambda *a, **k: [])
    monkeypatch.setattr(ku, 'sweep_once', lambda *a, **k: calls.append('notices') or [])
    monkeypatch.setattr(du, 'sweep_once', lambda **k: [])
    monkeypatch.setattr(nf, 'card_change_pass', list)
    monkeypatch.setattr(ng, 'guarantee_pass', list)
    monkeypatch.setattr(rn, 'retry_pending_killed_notices_all', list)
    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title='gate test', assignee='worker')
        notify.add_notify_sub(conn, task_id=tid, platform='tui', chat_id='chat')
    # Import lazily so RED exercises the original production host_pass.
    try:
        from tui_gateway import kanban_unloaded_gates as gates
    except ImportError:
        gates = None
    if gates is not None:
        gates.reset_for_tests()
    yield tid, live, calls
    if gates is not None:
        gates.reset_for_tests()


def test_host_skips_unchanged_structurally_empty_notices(host):
    _, _, calls = host
    ku.host_pass()
    ku.host_pass()
    assert calls == ['notices']


def test_host_rechecks_live_set_even_when_stores_do_not_change(host):
    _, live, calls = host
    live.add('chat')
    ku.host_pass()
    ku.host_pass()
    live.clear()
    ku.host_pass()
    assert calls == ['notices', 'notices']


def test_pending_notice_returning_empty_keeps_busy_retry_hot(host, monkeypatch, tmp_path):
    from hermes_state import SessionDB
    from tui_gateway import kanban_unloaded_gates as gates
    tid, _, calls = host
    # The chat exists (a busy retry); an absent chat is the orphan case below, which has nothing to retry.
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('chat', 'desktop')
    monkeypatch.setattr(gates, '_profile_paths', lambda every_store=True: [db.db_path])
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='ready')
    ku.host_pass()
    ku.host_pass()
    assert calls == ['notices', 'notices']


@pytest.mark.parametrize('busy', [False, True])
def test_host_deferred_guarantee_skips_only_until_backoff_expires(host, monkeypatch, tmp_path, busy):
    import json
    from hermes_state import SessionDB
    from tui_gateway import kanban_unloaded_gates as gates

    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('chat', 'desktop')
        db.append_message('chat', 'user', 'work')
        msg = db.append_kanban_notice('chat', 'done', {'notice_id': 'deferred'})
        closeout = {'state': 'deferred', 'retry_at': 5000}
        if busy:
            closeout['busy_attempts'] = 1
        db._write_rowcount('UPDATE messages SET timestamp=?, display_metadata=? WHERE id=?',
                          (1000, json.dumps({'closeout': closeout}), msg))
        monkeypatch.setattr(gates, '_profile_paths', lambda every_store=True: [db.db_path], raising=False)
        clock, calls = [4000.0], []
        monkeypatch.setattr(gates.time, 'time', lambda: clock[0])
        real = REAL_GUARANTEE
        @contextlib.contextmanager
        def opener(sid):
            yield db, sid
        def guarantee():
            calls.append(clock[0])
            return real(now=clock[0], stores=lambda: iter([db]), holders=list, session_db=opener,
                        wake=lambda *args: 'started', spawn=lambda job: job())
        monkeypatch.setattr(ng, 'guarantee_pass', guarantee)
        ku.host_pass()
        ku.host_pass()
        before_due = [4000.0, 4000.0] if busy else [4000.0]
        assert calls == before_due
        clock[0] = 5000.0
        ku.host_pass()
        assert calls == before_due + [5000.0]
        assert db._read_one('SELECT display_metadata FROM messages WHERE id=?', (msg,))[0].find('started') >= 0
    finally:
        db.close()


@pytest.mark.parametrize(('booked_sha', 'current_sha', 'timestamp', 'deadline'), [
    ('old-build', 'new-build', 1.0, 1500.0),
    ('old-build', 'old-build', 1.0, 9000.0),
    (None, 'new-build', 1.0, 9000.0),
    ('old-build', '', 1.0, 9000.0),
    ('old-build', 'new-build', 1400.0, 1700.0),
])
def test_host_guarantee_uses_effective_retry_deadline(
        host, monkeypatch, tmp_path, booked_sha, current_sha, timestamp, deadline):
    import json
    from hermes_state import SessionDB
    from hermes_cli import host_loop_lease
    from tui_gateway import kanban_unloaded_gates as gates

    monkeypatch.setattr(host_loop_lease, '_core_sha', lambda: current_sha)
    with contextlib.closing(SessionDB(tmp_path / 'state.db')) as db:
        db.create_session('chat', 'desktop')
        db.append_message('chat', 'user', 'work')
        msg = db.append_kanban_notice('chat', 'done', {'notice_id': 'effective-due'})
        closeout = {'state': 'deferred', 'retry_at': 9000,
                    'retry_short_at': 1500, 'retry_code_sha': booked_sha}
        db._write_rowcount('UPDATE messages SET timestamp=?, display_metadata=? WHERE id=?',
                          (timestamp, json.dumps({'closeout': closeout}), msg))
        monkeypatch.setattr(gates, '_profile_paths', lambda every_store=True: [db.db_path])
        clock, calls, wakes = [1000.0], [], []
        monkeypatch.setattr(gates.time, 'time', lambda: clock[0])

        @contextlib.contextmanager
        def opener(sid):
            yield db, sid

        def guarantee():
            calls.append(clock[0])
            return REAL_GUARANTEE(
                now=clock[0], stores=lambda: iter([db]), holders=list, session_db=opener,
                wake=lambda *args: wakes.append(args) or 'started', spawn=lambda job: job())

        monkeypatch.setattr(ng, 'guarantee_pass', guarantee)
        ku.host_pass()
        before = db._read_one('SELECT display_metadata FROM messages WHERE id=?', (msg,))[0]
        clock[0] = deadline - 1
        ku.host_pass()
        assert calls == [1000.0]
        assert not wakes
        assert db._read_one('SELECT display_metadata FROM messages WHERE id=?', (msg,))[0] == before
        clock[0] = deadline
        assert len(ng.candidates(db, clock[0])) == 1
        ku.host_pass()
        assert calls == [1000.0, deadline]
        assert len(wakes) == 1
        after = json.loads(db._read_one('SELECT display_metadata FROM messages WHERE id=?', (msg,))[0])
        assert after['closeout']['state'] == 'started'


def test_independent_empty_subpasses_skip_but_fanout_stays_hot(host, monkeypatch, tmp_path):
    import sqlite3
    from tui_gateway import kanban_unloaded_gates as gates
    from hermes_cli import kanban_origin

    path = tmp_path / 'empty.db'
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE async_delegations(state TEXT, delivery_state TEXT)')
        conn.execute('CREATE TABLE sessions(id TEXT, parent_session_id TEXT, end_reason TEXT, ended_at REAL)')
        conn.commit()
    monkeypatch.setattr(gates, '_profile_paths', lambda every_store=True: [path])
    monkeypatch.setattr(kanban_origin, '_hub_db', lambda: None)
    calls = []
    monkeypatch.setattr(ku, 'sweep_remote_once', lambda *a, **k: calls.append('remote') or [])
    monkeypatch.setattr(du, 'sweep_once', lambda: calls.append('delegation') or [])
    monkeypatch.setattr(rn, 'retry_pending_killed_notices_all', lambda: calls.append('killed') or [])
    monkeypatch.setattr(nf, 'card_change_pass', lambda: calls.append('fanout') or [])
    ku.host_pass()
    ku.host_pass()
    assert calls == ['remote', 'delegation', 'fanout', 'killed', 'fanout']


@pytest.mark.parametrize('kind', ['notices', 'remote_notices', 'delegation', 'card_change_pass',
                                  'killed_notices', 'guarantee_pass'])
def test_pending_empty_result_does_not_stamp_success(kind, monkeypatch):
    from tui_gateway import kanban_unloaded_gates as gates
    gates.reset_for_tests()
    monkeypatch.setattr(gates, 'probe', lambda *a, **k: gates.Inputs(pending=True))
    calls = []
    for _ in range(2):
        gates.run(kind, lambda: calls.append(kind) or [], live=set())
    assert calls == [kind, kind]
    assert kind not in gates._last


@pytest.mark.parametrize('blocked', ['busy', 'live', 'slots'])
def test_host_delivers_after_deferral_with_no_new_event(host, monkeypatch, tmp_path, blocked):
    import time
    from hermes_state import SessionDB
    from hermes_state_errors import SessionTurnLeaseLostError
    from tui_gateway.wake_slots import WakeSlots

    tid, live, _ = host
    db = SessionDB(tmp_path / 'chat.db')
    db.create_session('chat', 'desktop')
    db.append_message('chat', 'user', 'work')
    from tui_gateway import kanban_unloaded_gates as gates
    # The injected opener and the production lookup name the same store: the chat exists, so it is no orphan.
    monkeypatch.setattr(gates, '_profile_paths', lambda every_store=True: [db.db_path])
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='ready')
    now = time.time() + 60
    @contextlib.contextmanager
    def opener(sid):
        yield db, sid
    original_append = db.append_kanban_notice
    busy = [blocked == 'busy']
    def append(*args):
        if busy[0]:
            raise SessionTurnLeaseLostError('busy')
        return original_append(*args)
    monkeypatch.setattr(db, 'append_kanban_notice', append)
    monkeypatch.setenv('HERMES_KANBAN_UNLOADED_WAKE', '0' if blocked != 'slots' else '1')
    slots = WakeSlots(1)
    monkeypatch.setattr(ku, '_wake_slots', slots)
    if blocked == 'slots':
        assert slots.acquire()
    if blocked == 'live':
        live.add('chat')
    monkeypatch.setattr(ku, 'sweep_once', lambda ids, **kwargs: REAL_SWEEP(
        ids, now=now, session_db=opener, holders=list, wake=lambda *args: 'refused', spawn=lambda job: job()))
    try:
        ku.host_pass()
        assert db._read_one("SELECT COUNT(*) FROM messages WHERE display_kind='kanban_notice'")[0] == 0
        busy[0] = False
        live.clear()
        if blocked == 'slots':
            slots.release()
        ku.host_pass()
        assert db._read_one("SELECT COUNT(*) FROM messages WHERE display_kind='kanban_notice'")[0] == 1
    finally:
        db.close()


def test_delegation_quiet_proof_still_runs_300_second_recovery(host, monkeypatch):
    from tui_gateway import kanban_unloaded_gates as gates
    monkeypatch.setattr(gates, '_profile_paths', lambda every_store=True: [])
    clock, calls = [1000.0], []
    monkeypatch.setattr(gates.time, 'time', lambda: clock[0])
    monkeypatch.setattr(du, 'sweep_once', lambda: calls.append(clock[0]) or [])
    ku.host_pass()
    clock[0] = 1299.0
    ku.host_pass()
    assert calls == [1000.0]
    clock[0] = 1300.0
    ku.host_pass()
    assert calls == [1000.0, 1300.0]


def test_prework_key_does_not_hide_commit_during_delivery(monkeypatch):
    from tui_gateway import kanban_unloaded_gates as gates
    gates.reset_for_tests()
    revision, calls = [1], []
    monkeypatch.setattr(gates, 'probe', lambda *a, **k: gates.Inputs((revision[0],), pending=False))
    def raced():
        calls.append(revision[0])
        revision[0] += 1
        return []
    gates.run('notices', raced, live=set())
    gates.run('notices', raced, live=set())
    assert calls == [1, 2]


def test_failed_work_leaves_no_skip_stamp(monkeypatch):
    from tui_gateway import kanban_unloaded_gates as gates
    gates.reset_for_tests()
    monkeypatch.setattr(gates, 'probe', lambda *a, **k: gates.Inputs(('empty',), pending=False))
    def failed():
        raise RuntimeError('read failed')
    with pytest.raises(RuntimeError, match='read failed'):
        gates.run('notices', failed, live=set())
    assert 'notices' not in gates._last


def test_probe_failure_does_not_skip_host_work(host, monkeypatch):
    from tui_gateway import kanban_unloaded_gates as gates
    _, _, calls = host
    def failed():
        raise OSError('store unavailable')
    monkeypatch.setattr(gates, '_notices', failed)
    ku.host_pass()
    ku.host_pass()
    assert calls == ['notices', 'notices']


# --- orphan subs: a chat in no store has nothing to deliver to (t_01a1dfa94f2251cd) --------------------------------

@pytest.fixture
def orphan(host, monkeypatch, tmp_path):
    """A completed card whose subscriber chat 'chat' is in no profile store (the hub's t_01a1c070c0c00298 shape)."""
    from hermes_state import SessionDB
    from tui_gateway import kanban_unloaded_gates as gates
    tid, _live, calls = host
    db = SessionDB(tmp_path / 'state.db')
    monkeypatch.setattr(gates, '_profile_paths', lambda every_store=True: [db.db_path])
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='ready')
    yield tid, db, calls
    db.close()


def test_orphan_sub_does_not_hold_the_notice_sweep_hot(orphan):
    _, _, calls = orphan
    ku.host_pass()
    ku.host_pass()
    ku.host_pass()
    assert calls == ['notices']


def test_orphan_sub_runs_again_when_its_chat_appears(orphan):
    _, db, calls = orphan
    ku.host_pass()
    ku.host_pass()
    db.create_session('chat', 'desktop')
    ku.host_pass()
    ku.host_pass()
    assert calls == ['notices', 'notices', 'notices']  # hot once the chat exists: a busy retry stays pending


def test_orphan_sub_of_an_archived_card_still_runs(orphan):
    from hermes_cli.kanban_db_archive import archive_task
    tid, _, calls = orphan
    with contextlib.closing(kbc.connect()) as conn:
        archive_task(conn, tid)
    ku.host_pass()
    ku.host_pass()
    assert calls == ['notices', 'notices']  # the sweep drops an archived card's sub


def test_orphan_with_an_inflight_claim_stays_hot(orphan):
    tid, _, calls = orphan
    with contextlib.closing(kbc.connect()) as conn:
        with conn:
            conn.execute('UPDATE kanban_notify_subs SET inflight_from = 1, inflight_pid = 1 WHERE task_id = ?', (tid,))
    ku.host_pass()
    ku.host_pass()
    assert calls == ['notices', 'notices']  # dead-holder recovery is not structure


def test_orphan_probe_failure_stays_hot(orphan, monkeypatch):
    from tui_gateway import kanban_unloaded_gates as gates
    _, _, calls = orphan
    def unreadable(*a, **k):
        raise OSError('store unreadable')
    monkeypatch.setattr(gates, '_profile_paths', unreadable)
    ku.host_pass()
    ku.host_pass()
    assert calls == ['notices', 'notices']


# --- remote_notices: a replica commit that changes no owed notice is not work (t_01a1dfa94f2251cd) ------------------

@pytest.fixture
def remote_host(monkeypatch, tmp_path):
    from hermes_cli import kanban_origin
    from tui_gateway import kanban_remote_notices as remote
    from tui_gateway import kanban_replica_observer as observer
    from tui_gateway import kanban_unloaded_gates as gates

    replica = tmp_path / 'hub.db'
    replica.write_bytes(b'')
    ledger = tmp_path / 'remote-notices.db'
    stamp = [1]
    cards = [('macbook', 'default/t_1', {'id': 't_1', 'status': 'done', 'completed_at': 100.0},
              [{'profile': 'default', 'session': 'chat-a'}])]
    monkeypatch.setattr(kanban_origin, '_hub_db', lambda: replica)
    monkeypatch.setattr(kanban_origin, 'gateway_names', lambda: ['hub'])
    monkeypatch.setattr(remote, 'ledger_path', lambda: ledger)
    monkeypatch.setattr(observer, 'fingerprint', lambda path: ('stamp', stamp[0]))
    monkeypatch.setattr(remote, '_sweep_snapshot', lambda path, me: (list(cards), {}))
    tasks = [(1, 1, 1)]
    monkeypatch.setattr(gates, '_replica_tasks_signature', lambda path: tasks[0])
    stamp.append(tasks)
    rows = []  # task rows "written" since the last signature: (gw, key, task)
    monkeypatch.setattr(gates, '_replica_rows_after', lambda path, seq: list(rows))
    stamp.append(rows)
    gates.reset_for_tests()
    remote._ledger(ledger).close()

    def settle(session='chat-a', state=None, delivered=remote.DELIVERED, holder=None):
        with contextlib.closing(remote._ledger(ledger)) as conn, conn:
            conn.execute('INSERT OR REPLACE INTO notices (session, ref, state, at, delivered, holder_pid, notice_key) '
                         'VALUES (?,?,?,?,?,?,?)',
                         (session, 'macbook/default/t_1', state or remote._notice_state(cards[0][2]), 1.0,
                          delivered, holder, 'k'))
    return gates, cards, stamp, settle


def test_remote_probe_stays_hot_until_every_owed_notice_is_settled(remote_host):
    gates, _, _, settle = remote_host
    assert gates._remote().pending
    settle()
    assert not gates._remote().pending


def test_remote_probe_key_ignores_unrelated_replica_commits(remote_host):
    gates, _, stamp, settle = remote_host
    settle()
    first = gates._remote()
    stamp[0] += 1
    assert gates._remote() == first


def test_remote_probe_key_changes_with_the_card_state_and_the_ledger(remote_host):
    gates, cards, stamp, settle = remote_host
    settle()
    first = gates._remote()
    settle(session='chat-b')
    assert gates._remote().key != first.key
    cards[0] = (*cards[0][:2], {'id': 't_1', 'status': 'blocked', 'blocked_at': 200.0}, cards[0][3])
    stamp[1][0] = (1, 2, 2)  # the owner's write of that state
    stamp[2][:] = [('macbook', 'default/t_1', cards[0][2])]
    assert gates._remote().pending  # the new state has no settled row: it runs


def test_remote_probe_runs_when_an_unlinked_terminal_card_arrives(remote_host):
    """``observe`` records the first sighting of every terminal state, linked or not; skipping the pass that would
    record it lets a later link resurrect an expired notice (review of PR #3349)."""
    gates, _, stamp, settle = remote_host
    settle()
    first = gates._remote()
    assert not first.pending
    stamp[1][0] = (2, 2, 3)  # a task row arrived; no card the sweep owes a notice for changed
    stamp[2][:] = [('worker-1', 'default/t_9', {'status': 'done', 'completed_at': 5.0})]
    assert gates._remote().key != first.key
    stamp[1][0] = (2, 3, 4)  # ... and a blocked one
    stamp[2][:] = [('worker-1', 'default/t_8', {'status': 'blocked'})]
    second = gates._remote()
    assert second.key != first.key
    stamp[1][0] = (2, 4, 5)  # ... and a request withdrawn (an archived card)
    stamp[2][:] = [('worker-1', 'default/t_7', {'status': 'archived', 'withdrawal_notice': {'event_id': 3, 'kind': 'request_withdrawn', 'created_at': 1.0}})]
    from hermes_cli.kanban_withdrawal_notice import envelope
    assert envelope(stamp[2][0][2]) is not None
    assert gates._remote().key != second.key


def test_remote_probe_ignores_liveness_writes_to_cards_it_does_not_owe(remote_host):
    """The hub's replica rewrites several non-terminal task rows a second (claims, heartbeats, queue notes). None of
    them can add a terminal state to observe or change a swept card, so none moves the key (t_01a1dfa94f2251cd)."""
    gates, _, stamp, settle = remote_host
    settle()
    first = gates._remote()
    for n in range(2, 6):
        stamp[1][0] = (1 + 0, n, n)
        stamp[2][:] = [('worker-1', 'default/t_5', {'status': 'running'}), ('hub', 'default/t_6', {'status': 'ready'})]
        assert gates._remote() == first


def test_remote_probe_runs_when_a_swept_card_is_rewritten_or_unreadable(remote_host):
    gates, _, stamp, settle = remote_host
    settle()
    first = gates._remote()
    stamp[1][0] = (1, 2, 2)
    stamp[2][:] = [('macbook', 'default/t_1', {'status': 'ready'})]  # a reopened swept card
    assert gates._remote().key != first.key
    again = gates._remote()
    stamp[1][0] = (1, 3, 3)
    stamp[2][:] = [('worker-1', 'default/t_5', None)]  # undecodable
    assert gates._remote().key != again.key


def test_remote_probe_runs_when_rows_vanish_or_the_sequence_moves_backwards(remote_host):
    gates, _, stamp, settle = remote_host
    stamp[1][0] = (5, 9, 30)
    settle()
    first = gates._remote()
    stamp[1][0] = (4, 9, 29)
    assert gates._remote().key != first.key
    second = gates._remote()
    stamp[1][0] = (4, 3, 12)
    assert gates._remote().key != second.key
    third = gates._remote()
    stamp[1][0] = (7, 10, 40)  # three rows more but only one newer sequence
    stamp[2][:] = [('worker-1', 'default/t_5', {'status': 'running'})]
    assert gates._remote().key != third.key


def test_remote_probe_unreadable_new_rows_stay_hot(remote_host, monkeypatch):
    gates, _, stamp, settle = remote_host
    settle()
    assert not gates._remote().pending
    def boom(path, seq):
        raise OSError('replica unreadable')
    monkeypatch.setattr(gates, '_replica_rows_after', boom)
    stamp[1][0] = (1, 2, 2)
    assert gates._remote().pending


def test_remote_probe_open_chat_in_flight_claim_and_woken_stay_hot(remote_host):
    from tui_gateway import kanban_remote_notices as remote
    gates, _, _, settle = remote_host
    settle()
    assert not gates._remote().pending
    assert gates._remote(frozenset({'chat-a'})).pending
    settle(session='chat-b', delivered=0, holder=12345)
    assert gates._remote().pending
    settle(session='chat-b', delivered=remote.DELIVERED)
    with remote._woken_lock:
        remote._woken['x'] = 1
    try:
        assert gates._remote().pending
    finally:
        with remote._woken_lock:
            remote._woken.clear()


def test_remote_probe_unreadable_task_signature_stays_hot(remote_host):
    gates, _, stamp, settle = remote_host
    settle()
    stamp[1][0] = None
    assert gates._remote().pending


# --- an owed notice the sweep drops for good is not work (t_01a1dfa94f2251cd) ----------------------------------

@pytest.fixture
def observe_row(remote_host):
    """Record this host's observation of the fixture card's state (``first_seen`` None = seeded silently)."""
    from pathlib import Path

    from hermes_cli import kanban_origin
    from tui_gateway import kanban_remote_notices as remote
    gates, cards, _stamp, _settle = remote_host

    def put(first_seen):
        with contextlib.closing(remote._ledger(remote.ledger_path())) as conn, conn:
            conn.execute('CREATE TABLE IF NOT EXISTS observations (source TEXT, ref TEXT, state TEXT, first_seen REAL, '
                         'PRIMARY KEY(source, ref, state))')
            conn.execute('INSERT OR REPLACE INTO observations VALUES (?,?,?,?)',
                         (str(Path(kanban_origin._hub_db()).resolve()), 'macbook/default/t_1',
                          remote._notice_state(cards[0][2]), first_seen))
        gates._remote_memo.clear()
    return remote, gates, put


def test_remote_probe_expired_unobserved_notice_is_not_work(observe_row):
    remote, gates, put = observe_row
    assert gates._remote().pending  # never observed: the pass records it
    put(None)  # seeded by the source's first snapshot: the sweep counts it stale and delivers nothing
    assert not gates._remote().pending
    put(time.time() - remote.LATE_MAX_S - 3600)  # first seen past the late window
    assert not gates._remote().pending


def test_remote_probe_late_notice_is_still_work(observe_row):
    remote, gates, put = observe_row
    put(time.time() - 3600)  # observed an hour ago: past FRESH_S by the card's clock? not by the arrival clock
    assert gates._remote().pending
    put(time.time() - remote.FRESH_S - 3600)  # observed 7 h ago, inside the 72 h late window: delivered, late
    assert gates._remote().pending


@pytest.mark.parametrize('field', ['ident', 'generation', 'signature'])
def test_remote_identity_rearms_equal_aggregate_proofs(remote_host, monkeypatch, field):
    from tui_gateway import kanban_replica_observer as observer
    gates, _, _, settle = remote_host
    stamp = [observer.Fingerprint('replica', (1, 2), 3, 4, (100, 200))]
    monkeypatch.setattr(observer, 'fingerprint', lambda path: stamp[0])
    settle()
    before = gates._remote()
    stamp[0] = stamp[0]._replace(**{field: (9, 9) if field != 'generation' else 9})
    assert gates._remote().key != before.key


def test_remote_expiry_proof_rearms_on_backward_clock(observe_row, monkeypatch):
    remote, gates, put = observe_row
    clock = [remote.LATE_MAX_S + 2000]
    monkeypatch.setattr(gates.time, 'time', lambda: clock[0])
    put(1000)
    assert not gates._remote().pending
    clock[0] = 2000
    assert gates._remote().pending


def test_remote_concurrent_replica_write_does_not_publish(remote_host, monkeypatch):
    from tui_gateway import kanban_remote_notices as remote
    gates, _, stamp, settle = remote_host
    settle()
    original = remote._sweep_snapshot
    def racing(*args):
        result = original(*args)
        stamp[0] += 1
        return result
    monkeypatch.setattr(remote, '_sweep_snapshot', racing)
    assert gates._remote().pending
    assert 'inputs' not in gates._remote_memo
