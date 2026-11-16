"""H2/C11/R12 mutation-proof pins for ``kanban_unloaded_gates``.

Each test is written so one specific wrong gate (shared stamp, live set dropped, due ignored, pending stamped,
probe failure swallowed, reader identity dropped, ...) turns it red. See the mutation report for the matrix.
"""
import contextlib
import json
import sqlite3
import time

import pytest

from tui_gateway import delegation_unloaded as du
from tui_gateway import kanban_unloaded as ku
from tui_gateway import kanban_unloaded_gates as gates
from tui_gateway import notice_fanout as nf
from tui_gateway import notice_guarantee as ng
from tui_gateway import reap_notices as rn

KINDS = ['remote_notices', 'notices', 'delegation', 'card_change_pass', 'killed_notices', 'guarantee_pass']


@pytest.fixture(autouse=True)
def _clean():
    gates.reset_for_tests()
    yield
    gates.reset_for_tests()


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(gates.time, 'time', lambda: now[0])
    return now


@pytest.fixture
def counted(monkeypatch):
    """host_pass with every sub-pass stubbed to a counter and every probe a settable structural key."""
    calls = {k: 0 for k in KINDS}
    keys = {k: ('k', k) for k in KINDS}
    live = [set()]
    monkeypatch.setattr(ku, 'live_session_ids', lambda: set(live[0]))
    monkeypatch.setattr(ku, 'sweep_remote_once', lambda *a, **k: calls.__setitem__('remote_notices', calls['remote_notices'] + 1) or [])
    monkeypatch.setattr(ku, 'sweep_once', lambda *a, **k: calls.__setitem__('notices', calls['notices'] + 1) or [])
    monkeypatch.setattr(du, 'sweep_once', lambda *a, **k: calls.__setitem__('delegation', calls['delegation'] + 1) or [])
    monkeypatch.setattr(nf, 'card_change_pass', lambda: calls.__setitem__('card_change_pass', calls['card_change_pass'] + 1) or [])
    monkeypatch.setattr(rn, 'retry_pending_killed_notices_all', lambda: calls.__setitem__('killed_notices', calls['killed_notices'] + 1) or [])
    monkeypatch.setattr(ng, 'guarantee_pass', lambda: calls.__setitem__('guarantee_pass', calls['guarantee_pass'] + 1) or [])
    monkeypatch.setattr(gates, 'probe', lambda kind, **kw: gates.Inputs(keys[kind], pending=False))
    return calls, keys, live


# --------------------------------------------------------------------------------------- independence
def test_probe_returned_stamps_are_distinct_per_kind(monkeypatch):
    monkeypatch.setattr(gates, 'probe', lambda kind, **kw: gates.Inputs(('same',), pending=False))
    ran = []
    for kind in KINDS:
        gates.run(kind, lambda k=kind: ran.append(k) or [], live=set())
    assert ran == KINDS
    assert set(gates._last) == set(KINDS)
    for kind in KINDS:
        gates.run(kind, lambda k=kind: ran.append(k) or [], live=set())
    assert ran == KINDS  # each skipped on its own stamp


@pytest.mark.parametrize('hot', KINDS)
def test_pending_kind_never_disables_its_siblings_skip(monkeypatch, hot):
    monkeypatch.setattr(gates, 'probe', lambda kind, **kw: gates.Inputs(('s',), pending=(kind == hot)))
    ran = []
    for _ in range(2):
        for kind in KINDS:
            gates.run(kind, lambda k=kind: ran.append(k) or [], live=set())
    assert ran.count(hot) == 2
    assert all(ran.count(k) == 1 for k in KINDS if k != hot)


@pytest.mark.parametrize('moved', KINDS)
def test_host_pass_each_key_moves_only_its_own_subpass(counted, moved):
    calls, keys, _ = counted
    ku.host_pass()
    ku.host_pass()
    base = dict(calls)
    assert all(v == 1 for v in base.values()), base
    keys[moved] = ('moved', moved)
    ku.host_pass()
    for kind in KINDS:
        assert calls[kind] == base[kind] + (1 if kind == moved else 0), (kind, calls)


def test_host_pass_without_a_real_lease_never_skips_or_stamps(counted):
    calls, _, _ = counted
    for _ in range(3):
        ku.host_pass(every_store=False)
    assert gates._last == {}
    assert calls['notices'] == 3 and calls['delegation'] == 3 and calls['card_change_pass'] == 3
    assert calls['guarantee_pass'] == 3 and calls['remote_notices'] == 3


# ------------------------------------------------------------------------------------------ live set
@pytest.mark.parametrize('kind', KINDS)
def test_live_set_change_and_return_each_rerun(monkeypatch, kind):
    monkeypatch.setattr(gates, 'probe', lambda *a, **k: gates.Inputs(('s',), pending=False))
    ran = []
    work = lambda: ran.append(1) or []
    for live in (set(), set(), {'chat'}, {'chat'}, set(), {'chat', 'other'}, {'other', 'chat'}):
        gates.run(kind, work, live=live)
    # empty, (skip), chat, (skip), empty again, chat+other, (skip: order-insensitive)
    assert len(ran) == 4


def test_loaded_then_unloaded_chat_is_reconsidered_even_if_stores_never_change(monkeypatch):
    monkeypatch.setattr(gates, 'probe', lambda *a, **k: gates.Inputs(('s',), pending=False))
    ran = []
    work = lambda: ran.append(1) or []
    gates.run('notices', work, live={'c'})
    gates.run('notices', work, live={'c'})
    assert len(ran) == 1
    gates.run('notices', work, live=set())  # the chat unloaded: its deferred notice is deliverable now
    assert len(ran) == 2


def test_host_pass_notices_use_the_live_set_read_after_the_remote_wake(counted, monkeypatch):
    calls, _, _ = counted
    seq = iter([set(), {'woken'}, {'woken'}, {'woken'}])  # pass1: before/after remote; pass2: same
    monkeypatch.setattr(ku, 'live_session_ids', lambda: next(seq))
    ku.host_pass()
    ku.host_pass()
    # remote saw {} then {'woken'}: it must rerun; every later sub-pass saw {'woken'} twice: skip
    assert calls['remote_notices'] == 2
    assert all(calls[k] == 1 for k in KINDS if k != 'remote_notices'), calls


# --------------------------------------------------------------------------------------- due times
def test_stamp_expires_exactly_at_inputs_due(monkeypatch, clock):
    monkeypatch.setattr(gates, 'probe', lambda *a, **k: gates.Inputs(('s',), pending=False, due=1100.0))
    ran = []
    work = lambda: ran.append(clock[0]) or []
    gates.run('notices', work, live=set())
    clock[0] = 1099.999
    gates.run('notices', work, live=set())
    assert ran == [1000.0]
    clock[0] = 1100.0  # boundary is inclusive-due
    gates.run('notices', work, live=set())
    assert ran == [1000.0, 1100.0]


def test_earlier_stamped_due_still_bounds_a_later_probe_due(monkeypatch, clock):
    dues = iter([1100.0, 5000.0, 5000.0])
    monkeypatch.setattr(gates, 'probe', lambda *a, **k: gates.Inputs(('s',), pending=False, due=next(dues)))
    ran = []
    work = lambda: ran.append(clock[0]) or []
    gates.run('notices', work, live=set())   # stamps due 1100
    clock[0] = 1100.0
    gates.run('notices', work, live=set())   # probe says 5000, but the stamp's 1100 has passed
    assert ran == [1000.0, 1100.0]


def test_earlier_probe_due_beats_a_later_stamp(monkeypatch, clock):
    dues = iter([5000.0, 1050.0])
    monkeypatch.setattr(gates, 'probe', lambda *a, **k: gates.Inputs(('s',), pending=False, due=next(dues)))
    ran = []
    work = lambda: ran.append(clock[0]) or []
    gates.run('notices', work, live=set())
    clock[0] = 1050.0
    gates.run('notices', work, live=set())
    assert ran == [1000.0, 1050.0]


def test_already_due_input_is_never_stamped(monkeypatch, clock):
    monkeypatch.setattr(gates, 'probe', lambda *a, **k: gates.Inputs(('s',), pending=False, due=1000.0))
    ran = []
    for _ in range(2):
        gates.run('notices', lambda: ran.append(1) or [], live=set())
    assert len(ran) == 2 and 'notices' not in gates._last


def test_due_that_passes_during_work_is_not_stamped(monkeypatch, clock):
    monkeypatch.setattr(gates, 'probe', lambda *a, **k: gates.Inputs(('s',), pending=False, due=1010.0))
    ran = []
    def slow():
        ran.append(1)
        clock[0] = 1020.0
        return []
    gates.run('notices', slow, live=set())
    gates.run('notices', slow, live=set())
    assert len(ran) == 2


# ---- real guarantee/delegation due arithmetic
@pytest.fixture
def store(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('chat', 'desktop')
    db.append_message('chat', 'user', 'work')
    msg = db.append_kanban_notice('chat', 'done', {'notice_id': 'n1'})
    monkeypatch.setattr(gates, '_profile_paths', lambda every_store=True: [db.db_path])

    def seed(closeout, ts=1000.0, raw=None):
        meta = {} if closeout is ... else {'closeout': closeout}
        db._write_rowcount('UPDATE messages SET timestamp=?, display_metadata=? WHERE id=?',
                           (ts, raw if raw is not None else json.dumps(meta), msg))
    yield db, seed
    gates.reset_for_tests()
    db.close()


def test_guarantee_due_is_max_of_retry_and_grace(store, clock):
    _, seed = store
    clock[0] = 4950.0
    seed({'state': 'deferred', 'retry_at': 1000.0}, ts=4900.0)  # grace (4900+300=5200) dominates retry
    got = gates.probe('guarantee_pass')
    assert not got.pending and got.due == 5200.0
    seed({'state': 'deferred', 'retry_at': 9000.0}, ts=4900.0)  # retry dominates grace
    assert gates.probe('guarantee_pass').due == 9000.0


def test_guarantee_due_is_minimum_across_rows_and_stores(store, clock, tmp_path, monkeypatch):
    from hermes_state import SessionDB
    db, seed = store
    seed({'state': 'deferred', 'retry_at': 9000.0}, ts=1.0)
    other = SessionDB(tmp_path / 'other.db')
    try:
        other.create_session('c2', 'desktop')
        other.append_message('c2', 'user', 'x')
        m2 = other.append_kanban_notice('c2', 'done', {'notice_id': 'n2'})
        other._write_rowcount('UPDATE messages SET timestamp=1.0, display_metadata=? WHERE id=?',
                              (json.dumps({'closeout': {'state': 'deferred', 'retry_at': 7000.0}}), m2))
        monkeypatch.setattr(gates, '_profile_paths', lambda every_store=True: sorted([db.db_path, other.db_path]))
        got = gates.probe('guarantee_pass')
        assert not got.pending and got.due == 7000.0
        assert len(got.key) == 2
    finally:
        other.close()


def test_guarantee_row_becoming_due_with_no_commit_reruns(store, clock, monkeypatch):
    _, seed = store
    seed({'state': 'deferred', 'retry_at': 1500.0}, ts=1.0)
    ran = []
    work = lambda: ran.append(clock[0]) or []
    gates.run('guarantee_pass', work, live=set())
    clock[0] = 1499.0
    gates.run('guarantee_pass', work, live=set())
    clock[0] = 1500.0
    gates.run('guarantee_pass', work, live=set())
    assert ran == [1000.0, 1500.0]


def test_delegation_recovery_due_is_a_fixed_offset_from_now(store, clock):
    inputs = gates.probe('delegation')
    assert not inputs.pending and inputs.due == 1000.0 + du.RECOVER_EVERY_S
    assert gates.probe('killed_notices').due == float('inf')


# ------------------------------------------------------------------------------ busy / slot / pending hot
@pytest.mark.parametrize('closeout, ts', [
    (..., 1000.0),                                                        # never claimed (even inside grace)
    (..., 999999.0),
    ({'state': 'busy'}, 1000.0),                                          # legacy final busy: re-offered
    ({'state': 'deferred', 'retry_at': 1000.0, 'busy_attempts': 2}, 1.0),  # timed busy deferral
    ({'state': 'deferred', 'retry_at': 9e9, 'busy_attempts': 1}, 1.0),     # busy deferral, far future
    ({'state': 'deferred', 'retry_at': 500.0}, 1.0),                      # due now
    ({'state': 'deferred', 'retry_at': 1000.0}, 1.0),                     # due exactly now
    ({'state': 'deferred'}, 1.0),                                         # no retry_at
    ({'state': 'deferred', 'retry_at': True}, 1.0),                       # bool is not a time
    ({'state': 'deferred', 'retry_at': float('nan')}, 1.0),
    ({'state': 'deferred', 'retry_at': float('inf')}, 1.0),
    ({'state': 'deferred', 'retry_at': '9999'}, 1.0),
])
def test_guarantee_pending_shapes_stay_hot(store, clock, closeout, ts):
    _, seed = store
    seed(closeout, ts=ts)
    assert gates.probe('guarantee_pass').pending
    ran = []
    for _ in range(3):
        gates.run('guarantee_pass', lambda: ran.append(1) or [], live=set())
    assert len(ran) == 3 and 'guarantee_pass' not in gates._last


@pytest.mark.parametrize('raw', ['not json', '[1]', '{"closeout": "x"}', '{"closeout": 7}', '{"closeout": [1]}'])
def test_guarantee_malformed_metadata_stays_hot(store, raw):
    _, seed = store
    seed(None, raw=raw)
    assert gates.probe('guarantee_pass').pending


@pytest.mark.parametrize('closeout', [{'state': 'started'}, {'state': 'claimed', 'pid': 1},
                                       {'state': 'error'}, {'state': 'superseded'}, {'state': 'busy', 'busy_attempts': 8}])
def test_guarantee_settled_rows_are_the_only_ones_that_may_skip(store, closeout):
    _, seed = store
    seed(closeout)
    got = gates.probe('guarantee_pass')
    assert not got.pending and got.due == float('inf')


def test_slot_full_unclaimed_notice_is_retried_every_pass_then_delivered(store, clock, monkeypatch):
    from tui_gateway.wake_slots import WakeSlots
    db, seed = store
    seed(..., ts=1.0)  # unclaimed, older than GRACE
    clock[0] = 5000.0
    slots = WakeSlots(1)
    assert slots.acquire()
    monkeypatch.setattr(ng, '_wake_slots', slots)
    woken, seen = [], []
    @contextlib.contextmanager
    def opener(sid):
        yield db, sid
    def guarantee():
        seen.append(1)
        return REAL(now=5000.0, stores=lambda: iter([db]), holders=list, session_db=opener,
                    wake=lambda *a: woken.append(a) or 'started', spawn=lambda job: job())
    REAL = ng.guarantee_pass
    monkeypatch.setattr(ng, 'guarantee_pass', guarantee)
    for name in ('live_session_ids',):
        monkeypatch.setattr(ku, name, lambda: set())
    monkeypatch.setattr(ku, 'sweep_remote_once', lambda *a, **k: [])
    monkeypatch.setattr(ku, 'sweep_once', lambda *a, **k: [])
    monkeypatch.setattr(du, 'sweep_once', lambda *a, **k: [])
    monkeypatch.setattr(nf, 'card_change_pass', list)
    monkeypatch.setattr(rn, 'retry_pending_killed_notices_all', list)
    ku.host_pass()
    ku.host_pass()
    assert len(seen) == 2 and not woken
    slots.release()
    ku.host_pass()
    assert len(seen) == 3 and woken  # delivered with no store commit between the refusal and the retry


def test_remote_pending_inputs_stay_hot(monkeypatch, tmp_path):
    from hermes_cli import kanban_origin
    from tui_gateway import kanban_remote_notices as remote
    from tui_gateway import kanban_replica_observer as observer
    ledger = tmp_path / 'ledger.db'
    with contextlib.closing(sqlite3.connect(ledger)) as conn:
        conn.execute('CREATE TABLE notices(delivered INTEGER, holder_pid INTEGER)')
        conn.commit()
    monkeypatch.setattr(kanban_origin, '_hub_db', lambda: tmp_path / 'hub.db')
    monkeypatch.setattr(kanban_origin, 'gateway_names', lambda: ['me'])
    monkeypatch.setattr(observer, 'fingerprint', lambda p: ('fp', 1))
    monkeypatch.setattr(gates, '_replica_tasks_signature', lambda p: (1, 1, 1))
    monkeypatch.setattr(remote, '_sweep_snapshot', lambda p, me: ([], {}))
    monkeypatch.setattr(remote, 'ledger_path', lambda: ledger)
    remote._woken.clear()
    base = gates.probe('remote_notices')
    assert not base.pending and base.key[1] == 'me' and isinstance(base.key[2], str)  # a content digest, not the replica counter
    monkeypatch.setattr(remote, '_sweep_snapshot', lambda p, me: ([{'key': 'a'}], {}))
    gates._remote_memo.clear()  # the swept cards follow the replica's rows: this mock stands for a write
    assert gates.probe('remote_notices').pending  # even delivered terminal cards are hot
    monkeypatch.setattr(remote, '_sweep_snapshot', lambda p, me: ([], {}))
    gates._remote_memo.clear()
    remote._woken[('x',)] = {}
    try:
        assert gates.probe('remote_notices').pending  # a woken running flag is not a DB commit
    finally:
        remote._woken.clear()
    with contextlib.closing(sqlite3.connect(ledger)) as conn:
        conn.execute('INSERT INTO notices(delivered, holder_pid) VALUES (0, 123)')
        conn.commit()
    assert gates.probe('remote_notices').pending  # a HELD undelivered ledger row: stale-claim recovery
    monkeypatch.setattr(observer, 'fingerprint', lambda p: None)
    assert gates.probe('remote_notices').pending  # unknown replica state
    monkeypatch.setattr(kanban_origin, 'gateway_names', lambda: [''])
    monkeypatch.setattr(observer, 'fingerprint', lambda p: ('fp', 1))
    monkeypatch.setattr(gates, '_replica_tasks_signature', lambda p: (1, 1, 1))
    assert gates.probe('remote_notices').pending  # unknown gateway name


def test_remote_each_key_component_moves_the_key_independently(monkeypatch, tmp_path):
    from hermes_cli import kanban_origin
    from tui_gateway import kanban_remote_notices as remote
    from tui_gateway import kanban_replica_observer as observer
    ledger = tmp_path / 'ledger.db'
    with contextlib.closing(sqlite3.connect(ledger)) as conn:
        conn.execute('CREATE TABLE notices(delivered INTEGER, holder_pid INTEGER)')
        conn.execute('INSERT INTO notices(delivered) VALUES (1)')
        conn.commit()
    state = {'fp': ('fp', 1), 'me': 'me', 'ledger': ledger}
    monkeypatch.setattr(kanban_origin, '_hub_db', lambda: tmp_path / 'hub.db')
    monkeypatch.setattr(kanban_origin, 'gateway_names', lambda: [state['me']])
    monkeypatch.setattr(observer, 'fingerprint', lambda p: state['fp'])
    monkeypatch.setattr(gates, '_replica_tasks_signature', lambda p: (1, 1, 1))
    monkeypatch.setattr(remote, '_sweep_snapshot', lambda p, me: ([], {}))
    monkeypatch.setattr(remote, 'ledger_path', lambda: state['ledger'])
    remote._woken.clear()
    base = gates.probe('remote_notices')
    assert not base.pending
    assert gates.probe('remote_notices').key == base.key  # stable
    state['fp'] = ('fp', 2)  # the replica's commit counter churns on every sync: it is NOT a key component
    assert gates.probe('remote_notices').key == base.key
    monkeypatch.setattr(remote, '_sweep_snapshot', lambda p, me: ([('gw', 'k', {'status': 'done'}, [])], {}))
    gates._remote_memo.clear()  # the swept cards follow the replica's rows: this mock stands for a write
    assert gates.probe('remote_notices').key != base.key  # a swept card's content moves it
    monkeypatch.setattr(remote, '_sweep_snapshot', lambda p, me: ([], {}))
    gates._remote_memo.clear()
    state['fp'] = ('fp', 1)
    state['me'] = 'you'
    assert gates.probe('remote_notices').key != base.key
    state['me'] = 'me'
    with contextlib.closing(sqlite3.connect(ledger)) as conn:  # delivered-row commit: no pending, key moves
        conn.execute('INSERT INTO notices(delivered) VALUES (1)')
        conn.commit()
    after = gates.probe('remote_notices')
    assert not after.pending and after.key != base.key
    state['ledger'] = tmp_path / 'absent.db'
    missing = gates.probe('remote_notices')
    assert not missing.pending and missing.key != after.key


def test_delegation_and_killed_pending_inputs_stay_hot(tmp_path, monkeypatch):
    import tools.async_delegation_idle as idle
    path = tmp_path / 'state.db'
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE async_delegations(state TEXT, delivery_state TEXT)')
        conn.execute('CREATE TABLE sessions(id TEXT, parent_session_id TEXT, end_reason TEXT, ended_at REAL)')
        conn.commit()
    monkeypatch.setattr(gates, '_profile_paths', lambda every_store=True: [path])
    assert not gates.probe('delegation').pending and not gates.probe('killed_notices').pending
    monkeypatch.setattr(idle, 'ledger_quiet', lambda p: False)
    assert gates.probe('delegation').pending
    assert not gates.probe('killed_notices').pending  # independent of delegation
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO sessions VALUES ('c','p','startup_orphan_reap',?)", (time.time() - 60,))
        conn.commit()
    assert gates.probe('killed_notices').pending  # inside the retry window delivered ones stay hot too


# ---------------------------------------------------------------------------------- failure fallback
def _boom(*a, **k):
    raise sqlite3.OperationalError('unavailable')


@pytest.mark.parametrize('kind, target', [
    ('notices', ('hermes_cli.kanban_db', 'list_boards')),
    ('guarantee_pass', ('tui_gateway.kanban_unloaded_gates', '_profile_paths')),
    ('delegation', ('tui_gateway.kanban_unloaded_gates', '_profile_paths')),
    ('killed_notices', ('tui_gateway.kanban_unloaded_gates', '_profile_paths')),
    ('remote_notices', ('hermes_cli.kanban_origin', '_hub_db')),
    ('card_change_pass', ('tui_gateway.notice_fanout_state', 'path')),
])
def test_probe_failure_is_pending_and_work_runs_every_pass(monkeypatch, kind, target):
    import importlib
    gates.reset_for_tests()
    monkeypatch.setattr(importlib.import_module(target[0]), target[1], _boom)
    got = gates.probe(kind)
    assert got.pending and got.key == ()
    ran = []
    for _ in range(3):
        gates.run(kind, lambda: ran.append(1) or [], live=set())
    assert len(ran) == 3 and kind not in gates._last


@pytest.mark.parametrize('kind', KINDS)
def test_raising_work_clears_an_earlier_valid_stamp(monkeypatch, kind):
    monkeypatch.setattr(gates, 'probe', lambda *a, **k: gates.Inputs(('s',), pending=False))
    gates.run(kind, list, live=set())
    assert kind in gates._last
    def failed():
        raise RuntimeError('mid-pass')
    # same key would have skipped; a live change forces the work to run and raise
    with pytest.raises(RuntimeError):
        gates.run(kind, failed, live={'chat'})
    assert kind not in gates._last
    ran = []
    gates.run(kind, lambda: ran.append(1) or [], live={'chat'})  # same key as the failed run: must not skip
    assert ran == [1]


def test_missing_board_store_is_unprovable_not_empty():
    from hermes_cli.kanban_db_boards import create_board
    create_board('only-nondefault')  # default board file never created: reading it fails
    got = gates.probe('notices')
    assert got.pending and got.key == ()


def test_host_pass_failure_in_remote_does_not_stop_notices_and_leaves_no_stamp(counted, monkeypatch):
    calls, _, _ = counted
    def failed(*a, **k):
        raise RuntimeError('remote down')
    monkeypatch.setattr(ku, 'sweep_remote_once', failed)
    ku.host_pass()
    ku.host_pass()
    assert 'remote_notices' not in gates._last
    assert calls['notices'] == 1 and calls['guarantee_pass'] == 1  # others still ran and still skip on their stamp


def test_unexpected_probe_error_in_host_pass_runs_everything_each_time(counted, monkeypatch):
    calls, _, _ = counted
    monkeypatch.setattr(gates, 'probe', lambda *a, **k: gates.Inputs())  # what probe() returns on any exception
    for _ in range(2):
        ku.host_pass()
    assert all(v == 2 for v in calls.values()), calls


# ----------------------------------------------------------------------------------- reader identity
def test_revision_key_contains_the_reader_so_a_new_handle_never_matches(tmp_path):
    path = tmp_path / 'x.db'
    a = sqlite3.connect(path)
    b = sqlite3.connect(path)
    try:
        a.execute('CREATE TABLE t(x)')
        a.commit()
        assert gates._revision(a) == gates._revision(a)
        assert gates._revision(a) != gates._revision(b)  # same file, same counters, different handle
        assert gates._revision(a)[0] is a
    finally:
        a.close()
        b.close()


def test_notices_probe_key_is_independent_of_the_reader_handle(monkeypatch):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_reader_retention as retention
    from hermes_cli import kanban_db as kb
    with contextlib.closing(kbc.connect()) as conn:
        kb.create_task(conn, title='x', assignee='w')
    first = gates.probe('notices')
    assert not first.pending and gates.probe('notices').key == first.key  # kept handle reused
    real = retention.reader
    handles = []
    @contextlib.contextmanager
    def fresh(path):
        conn = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)
        handles.append(conn)
        try:
            yield conn
        finally:
            pass  # kept open so id() cannot be recycled
    monkeypatch.setattr(retention, 'reader', fresh)
    one, two = gates.probe('notices'), gates.probe('notices')
    assert not one.pending and not two.pending
    assert one.key == two.key  # the key is the (re-proved) empty candidate set, not a handle revision
    ran = []
    for _ in range(2):
        gates.run('notices', lambda: ran.append(1) or [], live=set())
    assert len(ran) == 1
    for h in handles:
        h.close()
    monkeypatch.setattr(retention, 'reader', real)


def test_guarantee_key_is_stable_on_the_kept_reader_across_passes(store):
    from tui_gateway.kanban_notice_session_store import profile_store, reader_of
    db, seed = store
    seed({'state': 'started'})
    first, second = gates.probe('guarantee_pass'), gates.probe('guarantee_pass')
    assert not first.pending and first.key == second.key
    with profile_store(db.db_path) as s1:
        r1 = reader_of(s1)
    with profile_store(db.db_path) as s2:
        r2 = reader_of(s2)
    assert r1 is r2
    ran = []
    for _ in range(2):
        gates.run('guarantee_pass', lambda: ran.append(1) or [], live=set())
    assert len(ran) == 1


def test_guarantee_commit_to_a_candidate_row_moves_the_key_but_an_unrelated_one_does_not(store):
    db, seed = store
    seed({'state': 'started'})
    before = gates.probe('guarantee_pass')
    other = sqlite3.connect(db.db_path)
    try:
        other.execute("INSERT INTO sessions(id, source, started_at) VALUES ('unrelated', 'desktop', 1.0)")
        other.commit()  # any chat write on a live install: not an input of this pass
        assert gates.probe('guarantee_pass').key == before.key
        other.execute("UPDATE messages SET display_metadata='{}' WHERE display_kind='kanban_notice'")
        other.commit()
    finally:
        other.close()
    after = gates.probe('guarantee_pass')
    assert after.pending or after.key != before.key


def test_guarantee_store_appearing_or_vanishing_moves_the_key(store, tmp_path, monkeypatch):
    db, seed = store
    seed({'state': 'started'})
    ghost = tmp_path / 'ghost.db'
    monkeypatch.setattr(gates, '_profile_paths', lambda every_store=True: sorted([db.db_path, ghost]))
    with_ghost = gates.probe('guarantee_pass')
    assert not with_ghost.pending and (str(ghost), None) in with_ghost.key
    monkeypatch.setattr(gates, '_profile_paths', lambda every_store=True: [db.db_path])
    assert gates.probe('guarantee_pass').key != with_ghost.key


# ----------------------------------------------------------- board components move key independently
def test_notices_second_board_change_moves_key_and_new_subscription_goes_hot(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as notify
    from hermes_cli.kanban_db_boards import create_board
    with contextlib.closing(kbc.connect()) as conn:
        kb.create_task(conn, title='default board exists', assignee='w')
    create_board('second')
    from hermes_state import SessionDB
    chat_db = SessionDB(tmp_path / 'chat-state.db')
    chat_db.create_session('chat', 'desktop')  # an existing chat: a sub whose chat is in no store is an orphan (inert)
    monkeypatch.setattr(gates, '_profile_paths', lambda every_store=True: [chat_db.db_path])
    base = gates.probe('notices')
    assert not base.pending and len(base.key) == 2
    with contextlib.closing(kbc.connect(board='second')) as conn:
        tid = kb.create_task(conn, title='y', assignee='w')
    moved = gates.probe('notices')
    assert not moved.pending and moved.key == base.key  # board bookkeeping/writes are not inputs
    with contextlib.closing(kbc.connect(board='second')) as conn:
        notify.add_notify_sub(conn, task_id=tid, platform='tui', chat_id='chat')
    quiet = gates.probe('notices')
    assert not quiet.pending and quiet.key == moved.key  # a sub on an open card is still an empty proof
    with contextlib.closing(kbc.connect(board='second')) as conn:
        kb.complete_task(conn, tid, summary='ready')
    assert gates.probe('notices').pending  # the unseen terminal event on the OTHER board goes hot


# ------------------------------------------------------------------ defence in depth (probe contract)
@pytest.mark.parametrize('kind', KINDS)
def test_pending_probe_never_skips_even_when_its_key_equals_a_live_stamp(monkeypatch, kind):
    state = {'pending': False}
    monkeypatch.setattr(gates, 'probe', lambda *a, **k: gates.Inputs(('same',), pending=state['pending']))
    ran = []
    gates.run(kind, lambda: ran.append(1) or [], live=set())
    assert kind in gates._last
    state['pending'] = True
    gates.run(kind, lambda: ran.append(1) or [], live=set())
    assert len(ran) == 2


def test_revision_sees_a_commit_made_through_the_same_handle(tmp_path):
    conn = sqlite3.connect(tmp_path / 'own.db')
    try:
        conn.execute('CREATE TABLE t(x)')
        conn.commit()
        before = gates._revision(conn)
        conn.execute('INSERT INTO t VALUES (1)')
        conn.commit()
        after = gates._revision(conn)
        assert after[1] == before[1]  # data_version only reflects OTHER connections' commits
        assert after != before  # total_changes still moves the key
    finally:
        conn.close()
