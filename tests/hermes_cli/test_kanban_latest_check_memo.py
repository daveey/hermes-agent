"""G2d: reuse the winning check reduction, not only its decoded input rows."""
import pytest

from hermes_cli import kanban_blocker_inputs as inputs
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_shared_blocker_checks as checks
from tests.hermes_cli import test_kanban_shared_blockers as helpers

kanban_home = helpers.kanban_home


def _seed(conn, *, rc=1, at=100):
    tid = kb.create_task(conn, title='check carrier', assignee='nobody')
    with kb.write_txn(conn):
        kb._append_event(conn, tid, checks.CHECK_EVENT,
                         {'blocker_id': 'test-wall', 'checked_at': at, 'rc': rc})
    return tid


def test_repeated_consumers_reduce_once_and_receive_private_results(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        _seed(conn)
        calls = []
        real = checks._check_payloads

        def payloads(*args):
            calls.append(1)
            return real(*args)
        monkeypatch.setattr(checks, '_check_payloads', payloads)

        @inputs.input_pass
        def exercise():
            first = checks._latest_results(conn, 100)
            assert first['test-wall']['rc'] == 1
            first['test-wall']['rc'] = 0
            assert checks._latest_results(conn, 100)['test-wall']['rc'] == 1
            assert checks._latest_results(conn, 100)['test-wall']['rc'] == 1
            assert len(calls) == 1
        exercise()
        assert inputs._SCOPE.get() is None


def test_local_peer_writes_and_rollback_invalidate(kanban_home):
    with kbc.connect_closing() as conn, kbc.connect_closing() as peer:
        tid = _seed(conn)

        @inputs.input_pass
        def exercise():
            assert checks._latest_results(conn, 100)['test-wall']['rc'] == 1
            with kb.write_txn(peer):
                kb._append_event(peer, tid, checks.CHECK_EVENT,
                                 {'blocker_id': 'test-wall', 'checked_at': 101, 'rc': 0})
            assert checks._latest_results(conn, 100)['test-wall']['rc'] == 0
            conn.execute('BEGIN')
            kb._append_event(conn, tid, checks.CHECK_EVENT,
                             {'blocker_id': 'test-wall', 'checked_at': 102, 'rc': 1})
            assert checks._latest_results(conn, 100)['test-wall']['rc'] == 1
            conn.rollback()
            assert checks._latest_results(conn, 100)['test-wall']['rc'] == 0
            with kb.write_txn(conn):
                kb._append_event(conn, tid, checks.CHECK_EVENT,
                                 {'blocker_id': 'test-wall', 'checked_at': 103, 'rc': 1})
            assert checks._latest_results(conn, 100)['test-wall']['rc'] == 1
        exercise()


def test_aliases_and_lookback_are_live_inputs(kanban_home, monkeypatch):
    from hermes_cli import config
    from hermes_cli.kanban_shared_blockers import _LOOKBACK_SECONDS
    cfg = {'kanban': {'shared_blocker_aliases': {}}}
    monkeypatch.setattr(config, 'load_config_readonly', lambda: cfg)
    with kbc.connect_closing() as conn:
        tid = _seed(conn)
        with kb.write_txn(conn):
            conn.execute('UPDATE task_events SET created_at=100 WHERE task_id=?', (tid,))

        @inputs.input_pass
        def exercise():
            assert set(checks._latest_results(conn, 100)) == {'test-wall'}
            cfg['kanban']['shared_blocker_aliases']['test-wall'] = 'new-wall'
            assert set(checks._latest_results(conn, 100)) == {'new-wall'}
            assert checks._latest_results(conn, 101 + _LOOKBACK_SECONDS) == {}
        exercise()


def test_unavailable_reduction_config_is_not_published(kanban_home, monkeypatch):
    from hermes_cli import config
    from hermes_cli.config_backend import ConfigBackendUnavailable
    cfg = {'kanban': {'shared_blocker_aliases': {'test-wall': 'new-wall'}}}
    monkeypatch.setattr(config, 'load_config_readonly', lambda: cfg)
    real, calls = inputs.config, []

    def transient():
        calls.append(1)
        if len(calls) == 1:
            raise ConfigBackendUnavailable('reducer only')
        return real()
    monkeypatch.setattr(inputs, 'config', transient)
    with kbc.connect_closing() as conn:
        _seed(conn)

        @inputs.input_pass
        def exercise():
            assert set(checks._latest_results(conn, 100)) == {'test-wall'}
            assert set(checks._latest_results(conn, 100)) == {'new-wall'}
            assert len(calls) >= 2
        exercise()


def test_peer_write_during_reduction_refuses_publication(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn, kbc.connect_closing() as peer:
        tid = _seed(conn)
        real, calls = checks._check_payloads, []

        def payloads(*args):
            value = real(*args)
            calls.append(1)
            if len(calls) == 1:
                with kb.write_txn(peer):
                    kb._append_event(peer, tid, checks.CHECK_EVENT,
                                     {'blocker_id': 'test-wall', 'checked_at': 101, 'rc': 0})
            return value
        monkeypatch.setattr(checks, '_check_payloads', payloads)

        @inputs.input_pass
        def exercise():
            assert checks._latest_results(conn, 100)['test-wall']['rc'] == 1
            assert checks._latest_results(conn, 100)['test-wall']['rc'] == 0
            assert len(calls) == 2
        exercise()


@pytest.mark.parametrize('order', [(0, 1), (1, 0)])
def test_same_second_failure_wins_on_cached_path(kanban_home, order):
    with kbc.connect_closing() as conn:
        for rc in order:
            _seed(conn, rc=rc)

        @inputs.input_pass
        def exercise():
            expected, trusted = checks._reduce_latest_results(conn, 100)
            assert trusted and expected['test-wall']['rc'] == 1
            assert checks._latest_results(conn, 100) == expected
            assert checks._latest_results(conn, 100) == expected
        exercise()


def test_failed_reduction_is_retried(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        _seed(conn)
        real, calls = checks._check_payloads, []

        def payloads(*args):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('read failed')
            return real(*args)
        monkeypatch.setattr(checks, '_check_payloads', payloads)

        @inputs.input_pass
        def exercise():
            with pytest.raises(RuntimeError, match='read failed'):
                checks._latest_results(conn, 100)
            assert checks._latest_results(conn, 100)['test-wall']['rc'] == 1
            assert len(calls) == 2
        exercise()
