"""Scoped shared-blocker inputs preserve fresh observations and avoid repeats."""
import pytest

from hermes_cli import kanban_blocker_inputs as inputs
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_shared_blocker_fence as fence
from hermes_cli import kanban_shared_blocker_checks as checks
from hermes_cli import kanban_shared_blockers as blockers
from tests.hermes_cli import test_kanban_shared_blockers as helpers

kanban_home = helpers.kanban_home


def test_real_due_pass_reads_assertion_cohort_once(kanban_home):
    with kbc.connect_closing() as conn:
        helpers._blocked_behind(conn, 'quota', blocker_id='github-rest-quota')
        now = blockers.blocker_states(conn)['github-rest-quota'].asserted_at + 1
        queries = []
        conn.set_trace_callback(queries.append)
        checks.run_due_checks(conn, cfg={'kanban': {'shared_blocker_checks': {'github-rest-quota': ''}}}, now=now)
        cohort = [q for q in queries if 'ORDER BY ended_at, id' in q]
        assert len(cohort) == 1
        events = [q for q in queries if "SELECT payload FROM task_events WHERE kind='shared_blocker_check'" in q]
        assert len(events) == 1


@pytest.mark.parametrize('mutation', ['commit', 'rollback', 'savepoint', 'external', 'aliases', 'clock'])
def test_scope_invalidates_changes(kanban_home, monkeypatch, mutation):
    from hermes_cli import config
    with kbc.connect_closing() as conn:
        conn.execute('CREATE TABLE memo_probe (value INTEGER)')
        conn.execute('INSERT INTO memo_probe VALUES (1)')
        conn.commit()
        cfg = {'kanban': {'shared_blocker_aliases': {}}}
        monkeypatch.setattr(config, 'load_config_readonly', lambda: cfg)
        calls = []

        def compute():
            calls.append(1)
            return {'rows': [r[0] for r in conn.execute('SELECT value FROM memo_probe')]}

        @inputs.input_pass
        def exercise():
            first = inputs.read('probe', conn, 10, compute)
            first['rows'].append(99)
            assert inputs.read('probe', conn, 10, compute) == {'rows': [1]}
            assert len(calls) == 1
            expected, now = [1], 10
            if mutation == 'external':
                with kbc.connect_closing() as peer:
                    peer.execute('UPDATE memo_probe SET value=2')
                    peer.commit()
                expected = [2]
            elif mutation == 'aliases':
                cfg['kanban']['shared_blocker_aliases']['old'] = 'new'
            elif mutation == 'clock':
                now = 11
            else:
                conn.execute('SAVEPOINT sample' if mutation == 'savepoint' else 'BEGIN')
                conn.execute('UPDATE memo_probe SET value=2')
                assert inputs.read('probe', conn, 10, compute) == {'rows': [2]}
                if mutation == 'commit':
                    conn.commit()
                    expected = [2]
                else:
                    conn.rollback()
            assert inputs.read('probe', conn, now, compute) == {'rows': expected}
            assert len(calls) >= 2
        exercise()
        before = len(calls)
        inputs.read('probe', conn, 10, compute)
        assert len(calls) == before + 1


def test_clock_boundary_and_payload_mutation(kanban_home, monkeypatch):
    clock = [1800000000]
    monkeypatch.setattr(checks.time, 'time', lambda: clock[0])
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_behind(conn, 'wall', blocker_id='github-rest-quota')
        with kb.write_txn(conn):
            kb._append_event(conn, tid, checks.CHECK_EVENT, {'blocker_id': 'github-rest-quota', 'active_runs': ['a']})
            conn.execute('UPDATE task_events SET created_at=? WHERE kind=?',
                         (clock[0] - blockers._LOOKBACK_SECONDS, checks.CHECK_EVENT))

        @inputs.input_pass
        def exercise():
            payloads = checks._check_payloads(conn, None)
            assert len(payloads) == 1
            payloads[0]['active_runs'].append('mutated')
            assert checks._check_payloads(conn, None)[0]['active_runs'] == ['a']
            clock[0] += 1
            assert checks._check_payloads(conn, None) == []
        exercise()


def test_latest_and_historical_winners_stay_distinct(kanban_home):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_behind(conn, 'wall', blocker_id='github-rest-quota')
        now = blockers.blocker_states(conn)['github-rest-quota'].asserted_at + 10
        token = checks._assertion_tokens(conn, now)['github-rest-quota']
        with kb.write_txn(conn):
            for checked, rc in [(now - 2, 0), (now - 1, 0), (now - 1, 1), (now + 1, 0)]:
                kb._append_event(conn, tid, checks.CHECK_EVENT, {
                    'blocker_id': 'github-rest-quota', 'checked_at': checked, 'rc': rc, 'assertion_id': token})

        def observe():
            return (checks._latest_results(conn, now), fence._winning_checks(conn, 'github-rest-quota', now))
        expected = observe()
        assert expected[0]['github-rest-quota']['checked_at'] == now + 1
        assert expected[1][now - 1]['rc'] == 1
        assert expected[1][now - 2]['rc'] == 0
        assert now + 1 not in expected[1]
        assert inputs.input_pass(observe)() == expected


@pytest.mark.parametrize('peer_write', [False, True])
def test_probe_reassertion_is_fresh(kanban_home, monkeypatch, peer_write):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_behind(conn, 'wall', blocker_id='test-wall')
        now = blockers.blocker_states(conn)['test-wall'].asserted_at + 10

        def mutate(target):
            target.execute('UPDATE task_runs SET ended_at=? WHERE task_id=?', (now, tid))
            target.commit()

        def command(_):
            if peer_write:
                with kbc.connect_closing() as peer:
                    mutate(peer)
            else:
                mutate(conn)
            return 0, 'recovered'

        monkeypatch.setattr(checks, '_run_check', command)
        assert checks.run_due_checks(conn, cfg={'kanban': {'shared_blocker_checks': {'test-wall': 'unused'}}}, now=now) == []
        assert conn.execute('SELECT COUNT(*) FROM task_events WHERE kind=?', (checks.CHECK_EVENT,)).fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM task_events WHERE kind=?', (checks.ATTEMPT_EVENT,)).fetchone()[0] == 1


def test_aliases_change_actual_tokens(kanban_home, monkeypatch):
    from hermes_cli import config
    cfg = {'kanban': {'shared_blocker_aliases': {}}}
    monkeypatch.setattr(config, 'load_config_readonly', lambda: cfg)
    with kbc.connect_closing() as conn:
        helpers._blocked_behind(conn, 'wall', blocker_id='test-wall')

        @inputs.input_pass
        def exercise():
            assert 'test-wall' in checks._assertion_tokens(conn, 2000000000)
            cfg['kanban']['shared_blocker_aliases']['test-wall'] = 'other-wall'
            tokens = checks._assertion_tokens(conn, 2000000000)
            assert 'other-wall' in tokens and 'test-wall' not in tokens
        # Fix the observation clock to the assertion window, not wall-clock age.
        conn.execute('UPDATE task_runs SET ended_at=2000000000')
        conn.commit()
        exercise()


def test_scope_exit_exception_and_nested_reuse(kanban_home):
    calls = []
    with kbc.connect_closing() as conn:
        def compute():
            calls.append(1)
            return ['value']

        @inputs.input_pass
        def nested():
            return inputs.read('sample', conn, 10, compute)

        @inputs.input_pass
        def outer():
            nested()
            nested()
            assert len(calls) == 1
            raise ValueError('stop')

        with pytest.raises(ValueError, match='stop'):
            outer()
        nested()
        assert len(calls) == 2


def test_same_connection_commit_without_intermediate_read(kanban_home):
    with kbc.connect_closing() as conn:
        conn.execute('CREATE TABLE values_probe (value INTEGER)')
        conn.execute('INSERT INTO values_probe VALUES (1)')
        conn.commit()
        # WAL keeps main/shm stat stable: total_changes must carry this write.
        def compute():
            return conn.execute('SELECT value FROM values_probe').fetchone()[0]

        @inputs.input_pass
        def exercise():
            assert inputs.read('v', conn, 10, compute) == 1
            conn.execute('UPDATE values_probe SET value=2')
            conn.commit()
            assert inputs.read('v', conn, 10, compute) == 2
        exercise()


def test_mid_read_write_refuses_publication(kanban_home):
    with kbc.connect_closing() as conn:
        conn.execute('CREATE TABLE race_probe (value INTEGER)')
        conn.execute('INSERT INTO race_probe VALUES (1)')
        conn.commit()
        calls = []

        def compute():
            result = conn.execute('SELECT value FROM race_probe').fetchone()[0]
            if not calls:
                with kbc.connect_closing() as peer:
                    peer.execute('UPDATE race_probe SET value=2')
                    peer.commit()
            calls.append(1)
            return result

        @inputs.input_pass
        def exercise():
            assert inputs.read('v', conn, 10, compute) == 1
            assert inputs.read('v', conn, 10, compute) == 2
            assert len(calls) == 2
        exercise()
