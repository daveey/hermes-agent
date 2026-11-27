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
