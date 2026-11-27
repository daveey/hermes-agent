"""R5 T1–T9: expired absent-ref claims retain session and alias custody."""
import contextlib
import logging
import sqlite3

import pytest

from tui_gateway import kanban_remote_notices as notices

NOW = 1_800_000_000.25
HOUR = 3600
REF = 'worker/default/default/t_expired'
STATE = 'blocked@1799712000'
CHAT = 'chat'


@pytest.fixture
def claim(tmp_path, monkeypatch):
    ledger = tmp_path / 'ledger.db'
    source = tmp_path / 'replica.db'
    monkeypatch.setattr(notices.time, 'time', lambda: NOW)
    monkeypatch.setattr(notices, '_holder_is_me', lambda pid: pid == 1)
    monkeypatch.setattr(notices, '_holder_alive', lambda pid, start: pid == 2)
    with contextlib.closing(notices._ledger(ledger)) as conn, conn:
        conn.execute('CREATE TABLE observations (source TEXT, ref TEXT, state TEXT, first_seen, PRIMARY KEY(source,ref,state))')
        conn.execute('INSERT INTO observations VALUES (?,?,?,?)', (str(source), REF, STATE, NOW - 80 * HOUR))
        conn.execute('INSERT INTO notices (session,ref,state,at,delivered,holder_pid,holder_start,notice_key) '
                     'VALUES (?,?,?,?,0,3,30,?)', (CHAT, REF, STATE, NOW - HOUR, 'original-key'))
    return ledger, source


def run(claim, states=None):
    ledger, source = claim
    return notices._settle_stale_claims(states or {}, source, ledger)


def row(claim):
    with contextlib.closing(notices._ledger(claim[0])) as conn:
        return dict(conn.execute('SELECT * FROM notices WHERE session=? AND ref=? AND state=?',
                                 (CHAT, REF, STATE)).fetchone())


def execute(claim, sql, args=()):
    with sqlite3.connect(claim[0]) as conn:
        conn.execute(sql, args)


def test_T1_expired_absent_claim_settles_once_with_warning(claim, caplog):
    with caplog.at_level(logging.WARNING, logger=notices.__name__):
        assert run(claim) == 1
        assert run(claim) == 0
    result = row(claim)
    assert (result['delivered'], result['settled'], result['holder_pid'], result['holder_start']) == (notices.SETTLED, 'expired', None, None)
    assert result['notice_key'] == 'original-key'
    with sqlite3.connect(claim[0]) as conn:
        assert conn.execute('SELECT replica,profile,session,notice_id,reason FROM swept').fetchall() == [
            (str(claim[1]), 'default', CHAT, 'original-key', 'expired')]
    warnings = [r for r in caplog.records if r.name == notices.__name__ and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert all(value in warnings[0].getMessage() for value in (REF, STATE, CHAT, 't_expired'))


def test_T1_settlement_clears_real_gate_pending_preserves_healthy_claim(claim, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import kanban_origin
    from tui_gateway import kanban_unloaded_gates as gates, kanban_replica_observer
    ledger, source = claim
    execute(claim, 'INSERT INTO notices (session,ref,state,at,delivered,holder_pid,holder_start) '
                    'VALUES (?,?,?,?,0,2,20)', ('healthy-chat', 'worker/default/default/t_healthy', STATE, NOW))
    with sqlite3.connect(ledger) as conn:
        before = conn.execute('SELECT * FROM notices WHERE session=?', ('healthy-chat',)).fetchone()
    monkeypatch.setattr(kanban_origin, '_hub_db', lambda: source)
    monkeypatch.setattr(kanban_origin, 'gateway_names', lambda: ('gw',))
    stamp = SimpleNamespace(ident='source', generation=1, signature=1)
    monkeypatch.setattr(kanban_replica_observer, 'fingerprint', lambda path: stamp)
    monkeypatch.setattr(notices, 'ledger_path', lambda: ledger)
    monkeypatch.setattr(notices, '_woken', set())
    monkeypatch.setattr(gates, '_replica_token', lambda *args: 1)
    monkeypatch.setattr(notices, '_sweep_snapshot', lambda *args: ([], {}))
    gates.reset_for_tests()
    try:
        assert gates._remote().pending
        assert run(claim) == 1
        assert not gates._remote().pending
        with sqlite3.connect(ledger) as conn:
            assert conn.execute('SELECT * FROM notices WHERE session=?', ('healthy-chat',)).fetchone() == before
    finally:
        gates.reset_for_tests()


@pytest.mark.parametrize(('state_age', 'observation_age', 'settled'), [(80, 7, False), (7, 80, True)])
def test_T2_observation_not_state_clock(claim, state_age, observation_age, settled):
    execute(claim, 'UPDATE notices SET at=?', (NOW - state_age * HOUR,))
    execute(claim, 'UPDATE observations SET first_seen=?', (NOW - observation_age * HOUR,))
    assert run(claim) == int(settled)
    assert row(claim)['delivered'] == (notices.SETTLED if settled else 0)


@pytest.mark.parametrize(('extra', 'settled'), [(0, False), (1, True)])
def test_T3_late_window_strict_boundary_fractional_origin(claim, extra, settled):
    execute(claim, 'UPDATE observations SET first_seen=?', (NOW - notices.LATE_MAX_S - extra,))
    assert run(claim) == int(settled)


@pytest.mark.parametrize('bad', [None, 'old', '123', b'old', float('inf'), float('-inf'), float('nan')])
def test_T4_unknown_seeded_malformed_observation_retained(claim, bad):
    execute(claim, 'UPDATE observations SET first_seen=?', (bad,))
    before = row(claim)
    assert run(claim) == 0
    assert row(claim) == before


@pytest.mark.parametrize('missing', ['table', 'exact', 'all'])
def test_T4_missing_observations_keep_claim(claim, missing):
    if missing == 'table':
        execute(claim, 'DROP TABLE observations')
    elif missing == 'exact':
        execute(claim, 'UPDATE observations SET ref=?', ('hub/default/default/t_expired',))
    else:
        execute(claim, 'DELETE FROM observations')
    before = row(claim)
    assert run(claim) == 0
    assert row(claim) == before


@pytest.mark.parametrize('pid', [1, 2])
def test_T5_self_and_live_holder_unchanged(claim, pid):
    execute(claim, 'UPDATE notices SET holder_pid=?', (pid,))
    before = row(claim)
    assert run(claim) == 0
    assert row(claim) == before


def test_T6_present_same_state_retains_existing_path(claim):
    before = row(claim)
    assert run(claim, {REF: (STATE, {})}) == 0
    assert row(claim) == before


def test_T6_swept_insert_failure_rolls_back(claim, monkeypatch, caplog):
    def fail(*args):
        raise sqlite3.OperationalError('injected swept failure')
    monkeypatch.setattr(notices, '_swept_row', fail)
    before = row(claim)
    assert run(claim) == 0
    assert row(claim) == before
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize('young', [NOW - 7 * HOUR, None, NOW - 80 * HOUR])
def test_T7_existing_alias_keeps_its_own_clock_through_real_persist(claim, young):
    from tui_gateway import kanban_remote_observations as observations
    ledger, source = claim
    execute(claim, 'INSERT INTO observations VALUES (?,?,?,?)',
            (str(source), 'hub/default/default/t_expired', STATE, young))
    # _persist must not retroactively replace a pre-existing younger alias with the old minimum.
    execute(claim, 'CREATE TABLE observation_sources (source TEXT PRIMARY KEY)')
    execute(claim, 'INSERT INTO observation_sources VALUES (?)', (str(source),))
    task = {'id': 't_expired', 'status': 'blocked', 'updated_at': 1799712000,
            'human_request': {'action': 'reply'}}
    # Exercise insert-only alias persistence with the actual state, not a made-up minimum reducer.
    monkey_state = observations._notice_state(task)
    execute(claim, 'UPDATE observations SET state=?', (monkey_state,))
    execute(claim, 'UPDATE notices SET state=?', (monkey_state,))
    observations._persist(str(source), [('hub', 'default/default/t_expired', task)], ledger, NOW)
    assert run(claim) == (1 if young == NOW - 80 * HOUR else 0)
    with sqlite3.connect(ledger) as conn:
        assert conn.execute('SELECT first_seen FROM observations WHERE ref=?',
                            ('hub/default/default/t_expired',)).fetchone()[0] == young


@pytest.mark.parametrize('dimension', ['source', 'board', 'profile'])
def test_T8_different_scope_same_task_id_retained(claim, dimension):
    source = str(claim[1]) + ('-other' if dimension == 'source' else '')
    profile = 'other' if dimension == 'profile' else 'default'
    board = 'other' if dimension == 'board' else 'default'
    alias = f'hub/{profile}/{board}/t_expired'
    execute(claim, 'INSERT INTO observations VALUES (?,?,?,?)', (source, alias, STATE, NOW - 80 * HOUR))
    before = row(claim)
    assert run(claim) == 0
    assert row(claim) == before


def test_T9_writer_lock_acquired_before_decisive_read(claim, monkeypatch):
    """An actual peer writer must fail after BEGIN and before the observation SELECT."""
    real_ledger = notices._ledger
    evidence = []
    class Connection:
        def __init__(self, conn):
            self.conn = conn
        def execute(self, sql, parameters=()):
            if sql.startswith('SELECT') and 'observations' in sql:
                with sqlite3.connect(claim[0], timeout=0) as peer:
                    try:
                        peer.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?)',
                                     (str(claim[1]), 'hub/default/default/t_expired', STATE, NOW - HOUR))
                    except sqlite3.OperationalError as error:
                        assert 'locked' in str(error)
                        evidence.append('blocked')
                    else:
                        evidence.append('inserted')
            return self.conn.execute(sql, parameters)
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def __enter__(self):
            self.conn.__enter__()
            return self
        def __exit__(self, *args):
            return self.conn.__exit__(*args)
    monkeypatch.setattr(notices, '_ledger', lambda path: Connection(real_ledger(path)))
    assert run(claim) == 1
    assert evidence and set(evidence) == {'blocked'}
    # Once the settlement commits, an unrelated healthy writer progresses normally.
    execute(claim, 'INSERT INTO observations VALUES (?,?,?,?)',
            (str(claim[1]), 'hub/default/default/t_healthy', STATE, NOW))


# ---- additional R5 coverage: T4 errors, T5 holder race, T7 public custody, T9 two-connection races ----
import pathlib
import threading


def blocked_peer_insert(claim, ready, outcome):
    """Prove lock exclusion before attempting the ordinary waiting writer."""
    with sqlite3.connect(claim[0], timeout=0) as conn:
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            conn.execute('INSERT INTO observations VALUES (?,?,?,?)',
                         (str(claim[1]), HUB_ALIAS, STATE, NOW))
    ready.set()
    with sqlite3.connect(claim[0], timeout=5) as conn:
        conn.execute('INSERT INTO observations VALUES (?,?,?,?)',
                     (str(claim[1]), HUB_ALIAS, STATE, NOW))
        outcome.append('inserted')

HUB_ALIAS = 'hub/default/default/t_expired'


def hooked(monkeypatch, before_begin=None, on_select=None, fail=None, busy_zero=False):
    """Wrap the real ledger connection; hooks run real peer SQLite connections at exact points."""
    real_ledger = notices._ledger

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, parameters=()):
            if sql == 'BEGIN IMMEDIATE' and before_begin:
                before_begin()
            if fail and fail in sql:
                raise sqlite3.OperationalError('injected read failure')
            if sql.startswith('SELECT') and 'FROM observations' in sql and on_select:
                on_select()
            return self.conn.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def __enter__(self):
            self.conn.__enter__()
            return self

        def __exit__(self, *args):
            return self.conn.__exit__(*args)

    def opened(path):
        conn = real_ledger(path)
        if busy_zero:
            conn.execute('PRAGMA busy_timeout=0')
        return Connection(conn)
    monkeypatch.setattr(notices, '_ledger', opened)
    return real_ledger


def swept_count(claim):
    with sqlite3.connect(claim[0]) as conn:
        try:
            return conn.execute('SELECT COUNT(*) FROM swept').fetchone()[0]
        except sqlite3.OperationalError:  # no table: nothing was ever swept
            return 0


def no_warning(caplog):
    return not [r for r in caplog.records if r.levelno >= logging.WARNING]


def row_for(claim, ref):
    with contextlib.closing(notices._ledger(claim[0])) as conn:
        return dict(conn.execute('SELECT * FROM notices WHERE session=? AND ref=?', (CHAT, ref)).fetchone())


@pytest.mark.parametrize('ref', ['bad', 'worker/default/default', 'worker//default/t_expired',
                                 'worker/default/default/not_task', 'worker/default/default/t_expired/extra'])
def test_T4_malformed_refs_retained(claim, ref, caplog):
    execute(claim, 'UPDATE notices SET ref=?', (ref,))
    execute(claim, 'UPDATE observations SET ref=?', (ref,))
    before = row_for(claim, ref)
    assert run(claim) == 0
    assert row_for(claim, ref) == before
    assert no_warning(caplog) and swept_count(claim) == 0


@pytest.mark.parametrize('fail', ['SELECT session, ref, state, holder_pid, holder_start, notice_key FROM notices',
                                  'FROM notices WHERE session=? AND ref=? AND state=?',
                                  'FROM observations', 'FROM notices WHERE session=? AND state=?'])
def test_T4_read_errors_retained_without_exception(claim, monkeypatch, caplog, fail):
    before = row(claim)
    real = hooked(monkeypatch, fail=fail)
    assert run(claim) == 0
    monkeypatch.setattr(notices, '_ledger', real)
    assert row(claim) == before and no_warning(caplog) and swept_count(claim) == 0
    assert run(claim) == 1  # the error poisoned nothing


def test_T4_writer_lock_error_retained(claim, monkeypatch, caplog):
    before = row(claim)
    real = hooked(monkeypatch, busy_zero=True)
    peer = sqlite3.connect(claim[0], timeout=0, isolation_level=None)
    try:
        peer.execute('BEGIN IMMEDIATE')
        assert run(claim) == 0
    finally:
        peer.execute('ROLLBACK')
        peer.close()
    monkeypatch.setattr(notices, '_ledger', real)
    assert row(claim) == before and no_warning(caplog) and swept_count(claim) == 0
    assert run(claim) == 1  # same claim settles once the lock is gone


@pytest.mark.parametrize('replacement', [(4, 40), (3, 31), (2, 30), (1, 30)])
def test_T5_holder_replaced_between_selection_and_transaction(claim, monkeypatch, caplog, replacement):
    """Selection reads (3,30); a real peer replaces pid and/or start before BEGIN IMMEDIATE."""
    hooked(monkeypatch, before_begin=lambda: execute(
        claim, 'UPDATE notices SET holder_pid=?, holder_start=?', replacement))
    assert run(claim) == 0
    current = row(claim)
    assert (current['holder_pid'], current['holder_start'], current['delivered']) == (*replacement, 0)
    assert current['settled'] is None
    assert no_warning(caplog) and swept_count(claim) == 0


def test_T5_claim_settled_by_peer_before_transaction_not_resettled(claim, monkeypatch, caplog):
    hooked(monkeypatch, before_begin=lambda: execute(
        claim, 'UPDATE notices SET delivered=?, holder_pid=NULL, holder_start=NULL, settled=?',
        (notices.DELIVERED, 'peer')))
    assert run(claim) == 0
    assert (row(claim)['delivered'], row(claim)['settled']) == (notices.DELIVERED, 'peer')
    assert no_warning(caplog) and swept_count(claim) == 0


# --- T7: actual _persist plus public collect, two chats ---
OTHER = 'other-chat'


def card():
    from tui_gateway import kanban_remote_observations as observations
    task = {'id': 't_expired', 'status': 'blocked', 'title': 'x', 'board': 'default', 'assignee': 'default',
            'run': {'ended_at': NOW - 90 * HOUR, 'summary': 's'}, 'block_kind': 'needs_input',
            'sessions': [{'gw': 'macbook', 'profile': 'default', 'session': s, 'rel': 'shepherd'}
                         for s in (CHAT, OTHER)]}
    return observations._notice_state(task), task


@pytest.fixture
def two(claim):
    """Two chats hold dead worker-alias claims; the hub alias is the card still present."""
    state, task = card()
    execute(claim, 'DELETE FROM notices WHERE 1=1')
    for chat in (CHAT, OTHER):
        execute(claim, 'INSERT INTO notices (session,ref,state,at,delivered,holder_pid,holder_start,notice_key) '
                       'VALUES (?,?,?,?,0,3,30,?)', (chat, REF, state, NOW - HOUR, f'key-{chat}'))
    return claim, state, task


def seed(claim, state, worker, hub):
    execute(claim, 'DELETE FROM observations WHERE 1=1')
    for ref, age in ((REF, worker), (HUB_ALIAS, hub)):
        execute(claim, 'INSERT INTO observations VALUES (?,?,?,?)',
                (str(claim[1]), ref, state, None if age is None else NOW - age * HOUR))


def custody(claim, state):
    with contextlib.closing(notices._ledger(claim[0])) as conn:
        return {r['session']: (r['delivered'], r['settled'], r['holder_pid'])
                for r in conn.execute('SELECT * FROM notices WHERE ref=? AND state=?', (REF, state))}


@pytest.mark.parametrize(('worker', 'hub', 'settled'), [(80, 7, False), (80, None, False), (80, 80, True)])
def test_T7_real_persist_and_public_collect_two_session_custody(two, tmp_path, worker, hub, settled):
    from tests.tui_gateway.test_kanban_remote_arrival import _hub
    from tui_gateway import kanban_remote_observations as observations
    (ledger, source), state, task = two
    seed(two[0], state, worker, hub)
    # actual _persist over PRE-EXISTING aliases is insert-only: no clock is rewritten
    observations._persist(str(source), [('hub', 'default/default/t_expired', task)], ledger, NOW)
    with sqlite3.connect(ledger) as conn:
        clocks = dict(conn.execute('SELECT ref, first_seen FROM observations'))
    assert clocks == {REF: NOW - worker * HOUR, HUB_ALIAS: None if hub is None else NOW - hub * HOUR}
    assert notices._settle_stale_claims({HUB_ALIAS: (state, task)}, source, ledger) == (2 if settled else 0)
    for chat, (delivered, how, _pid) in custody(two[0], state).items():
        assert (delivered, how) == ((notices.SETTLED, 'expired') if settled else (0, None)), chat
    # public collect on the hub card, per chat, against a control ledger that never had the worker claims
    replica = _hub(pathlib.Path(tmp_path), [{'key': 'default/default/t_expired', 'task': task}])
    control = pathlib.Path(tmp_path) / 'control.db'
    with contextlib.closing(sqlite3.connect(ledger)) as src, contextlib.closing(sqlite3.connect(control)) as dst:
        src.backup(dst)
    execute((control, source), 'DELETE FROM notices WHERE ref=?', (REF,))

    def outcome(path, chat):
        notices._reset_for_tests()
        offered = notices.collect(chat, 'default', hub_db=replica, ledger=path, now=NOW)
        with contextlib.closing(notices._ledger(path)) as conn:
            rows = conn.execute('SELECT ref, delivered, settled FROM notices WHERE session=? AND ref!=? '
                                'ORDER BY ref', (chat, REF)).fetchall()
        return len(offered), [tuple(r) for r in rows]
    for chat in (CHAT, OTHER):
        if settled:  # all aliases expired: the hub alias is covered by the settled worker alias (moved)
            offered, rows = outcome(ledger, chat)
            assert offered == 0 and all(r[1] == notices.SETTLED for r in rows)
        else:  # candidate retained the worker claim: public collect behaves exactly as without it
            assert outcome(ledger, chat) == outcome(control, chat), chat
            assert custody(two[0], state)[chat] == (0, None, 3)


def test_T7_live_holder_keeps_only_its_own_session(two):
    (ledger, source), state, _task = two
    seed(two[0], state, 80, 80)
    execute(two[0], 'UPDATE notices SET holder_pid=2, holder_start=20 WHERE session=?', (OTHER,))
    assert notices._settle_stale_claims({}, source, ledger) == 1
    after = custody(two[0], state)
    assert after[OTHER] == (0, None, 2)
    assert after[CHAT] == (notices.SETTLED, 'expired', None)
    with sqlite3.connect(ledger) as conn:
        assert [r[0] for r in conn.execute('SELECT session FROM swept')] == [CHAT]


def test_T7_independent_clocks_per_session_alias_vetoes_all(two):
    """A young OTHER-session alias claim does not exist in observations: its scope is unproved => keep."""
    (ledger, source), state, _task = two
    seed(two[0], state, 80, 80)
    execute(two[0], 'INSERT INTO notices (session,ref,state,at,delivered,holder_pid,holder_start) '
                    'VALUES (?,?,?,?,0,3,30)', (CHAT, HUB_ALIAS, state, NOW))  # alias claim, observation present
    # CHAT's hub alias has an exact expired observation and dead holder: both CHAT claims may settle
    assert notices._settle_stale_claims({}, source, ledger) == 3
    execute(two[0], 'DELETE FROM observations WHERE ref=?', (HUB_ALIAS,))
    execute(two[0], 'UPDATE notices SET delivered=0, settled=NULL, holder_pid=3, holder_start=30')
    # CHAT's alias claim lacks an observation: its worker claim is kept; OTHER (no alias) still settles
    assert notices._settle_stale_claims({}, source, ledger) == 1
    assert custody(two[0], state)[CHAT] == (0, None, 3)
    assert custody(two[0], state)[OTHER][1] == 'expired'


# --- T9: real two-connection races ---
def test_T9_young_alias_inserted_before_acquisition_vetoes(claim, monkeypatch, caplog):
    before = row(claim)
    hooked(monkeypatch, before_begin=lambda: execute(
        claim, 'INSERT INTO observations VALUES (?,?,?,?)', (str(claim[1]), HUB_ALIAS, STATE, NOW - HOUR)))
    assert run(claim) == 0
    assert row(claim) == before and no_warning(caplog) and swept_count(claim) == 0


def test_T9_alias_claim_inserted_before_acquisition_vetoes(claim, monkeypatch, caplog):
    """A peer's new live alias claim and its expired observation precede the transaction."""
    def alias():
        execute(claim, 'INSERT INTO observations VALUES (?,?,?,?)', (str(claim[1]), HUB_ALIAS, STATE, NOW - 80 * HOUR))
        execute(claim, 'INSERT INTO notices (session,ref,state,at,delivered,holder_pid,holder_start) '
                       'VALUES (?,?,?,?,0,2,20)', (CHAT, HUB_ALIAS, STATE, NOW))
    before = row(claim)
    hooked(monkeypatch, before_begin=alias)
    assert run(claim) == 0
    assert row(claim) == before and no_warning(caplog) and swept_count(claim) == 0


def test_T9_after_acquisition_peer_alias_waits_then_wins_after_commit(claim, monkeypatch):
    """A real peer thread inserts a young alias after BEGIN IMMEDIATE: it blocks, then lands after the commit."""
    outcome, ready = [], threading.Event()
    thread = threading.Thread(target=blocked_peer_insert, args=(claim, ready, outcome))

    def start_once():
        if not ready.is_set():
            thread.start()
            assert ready.wait(2), 'peer did not observe the held writer lock'
            assert thread.is_alive() and not outcome
    hooked(monkeypatch, on_select=start_once)
    assert run(claim) == 1  # decision made while the young alias could not yet exist
    thread.join(10)
    assert outcome == ['inserted']
    assert row(claim)['settled'] == 'expired'
    with sqlite3.connect(claim[0]) as conn:
        assert conn.execute('SELECT COUNT(*) FROM observations WHERE ref=?', (HUB_ALIAS,)).fetchone()[0] == 1
    assert run(claim) == 0


def test_T9_after_acquisition_rollback_releases_peer(claim, monkeypatch):
    outcome, ready = [], threading.Event()
    thread = threading.Thread(target=blocked_peer_insert, args=(claim, ready, outcome))

    def start():
        if not ready.is_set():
            thread.start()
            assert ready.wait(2), 'peer did not observe the held writer lock'

    def fail(*args):
        raise sqlite3.OperationalError('injected')
    monkeypatch.setattr(notices, '_swept_row', fail)
    hooked(monkeypatch, on_select=start)
    before = row(claim)
    assert run(claim) == 0
    thread.join(10)
    assert outcome == ['inserted'] and row(claim) == before
