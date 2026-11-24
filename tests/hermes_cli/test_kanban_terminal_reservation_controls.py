"""Final-run reservation must use all budget limits and both timer paths."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import test_kanban_wake_check as helpers

kanban_home = helpers.kanban_home
_running, _probe, _events = helpers._running, helpers._probe, helpers._events


def _park_one_run_left(conn, tmp_path, *, remote, timed):
    tid = _running(conn)
    budget.set_override(conn, tid, runs=1, by='owner', reason='terminal settlement')
    cmd = _probe(tmp_path / 'terminal')
    assert kb.schedule_task(conn, tid, reason='SCHEDULED_UNTIL=2099-01-01T00:00:00Z', wake_check=cmd)
    with kb.write_txn(conn):
        payload = {'wake_check': cmd, 'probe_host': 'away' if remote else None}
        if timed:
            payload['wake_at'] = 1000
        kb._append_event(conn, tid, 'scheduled', payload)
        if remote:
            conn.execute('DELETE FROM task_wake_checks WHERE task_id=?', (tid,))
    return tid


def test_implied_local_timer_keeps_last_run_and_backs_off(kanban_home, tmp_path):
    from hermes_cli.kanban_continuation import _wake_parks
    now = 4070908801
    with kbc.connect_closing() as conn:
        tid = _park_one_run_left(conn, tmp_path, remote=False, timed=False)
        woken, pending = _wake_parks(conn, now)
        assert woken == [] and kb.get_task(conn, tid).status == 'scheduled'
        assert len(_events(conn, tid, 'terminal_run_reserved')) == 1
        assert pending == now + 120  # an expired implied timer must not stay hot


def test_timed_relayed_park_keeps_last_run(kanban_home, tmp_path):
    from hermes_cli.kanban_continuation import _wake_parks
    with kbc.connect_closing() as conn:
        tid = _park_one_run_left(conn, tmp_path, remote=True, timed=True)
        woken, pending = _wake_parks(conn, 2000)
        assert woken == [] and kb.get_task(conn, tid).status == 'scheduled'
        assert len(_events(conn, tid, 'terminal_run_reserved')) == 1
        assert pending == 2120


def test_implicit_relayed_park_is_not_reserved(kanban_home, tmp_path):
    # restore_missing_checks only repairs timed parks, so a reservation here could never be repaired.
    from hermes_cli.kanban_continuation import _wake_parks
    with kbc.connect_closing() as conn:
        tid = _park_one_run_left(conn, tmp_path, remote=True, timed=False)
        woken, _ = _wake_parks(conn, 4070908801)
        assert woken == [tid] and kb.get_task(conn, tid).status != 'scheduled'
        assert not _events(conn, tid, 'terminal_run_reserved')


def test_foreign_copy_never_reserves_with_worker_defaults(kanban_home, tmp_path, monkeypatch):
    from hermes_cli.kanban_terminal_run import reserve
    with kbc.connect_closing() as conn:
        tid = _park_one_run_left(conn, tmp_path, remote=False, timed=True)
        # Partial override (cache only): the run limit comes from this node's default.
        budget.set_override(conn, tid, cache_read_tokens=0, by='owner', reason='partial')
        monkeypatch.setattr(budget, 'limits', lambda cfg=None: budget.Limits(1, 0))
        with kb.write_txn(conn):
            conn.execute('CREATE TABLE IF NOT EXISTS kanban_sync_state (board_id TEXT PRIMARY KEY, cursor INTEGER, '
                         'gateway_id TEXT, hlc_last TEXT, mode TEXT, applying INTEGER, pending_run_uid TEXT)')
            conn.execute("INSERT INTO kanban_sync_state VALUES ('b',0,'home','h','m',0,NULL)")
            if 'created_on' not in [r[1] for r in conn.execute('PRAGMA table_info(tasks)')]:
                conn.execute('ALTER TABLE tasks ADD COLUMN created_on TEXT')
            conn.execute("UPDATE tasks SET created_on='home' WHERE id=?", (tid,))
        with kb.write_txn(conn):
            assert reserve(conn, tid, 'continuation_woken')  # canonical home: exactly one run left
        with kb.write_txn(conn):
            conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='terminal_run_reserved'", (tid,))
            conn.execute("UPDATE tasks SET created_on='worker-node' WHERE id=?", (tid,))
        with kb.write_txn(conn):
            assert not reserve(conn, tid, 'continuation_woken')  # worker's copy: defaults are not authoritative
        assert not _events(conn, tid, 'terminal_run_reserved')


def test_cache_cap_exhaustion_is_not_a_reserved_authorized_run(kanban_home, tmp_path):
    from hermes_cli.kanban_terminal_run import reserve
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        budget.set_override(conn, tid, runs=2, cache_read_tokens=10, by='owner', reason='bounded')
        with kb.write_txn(conn):
            conn.execute('UPDATE task_runs SET started_at=? WHERE task_id=?', (int(time.time()) - 120, tid))
        assert kb.schedule_task(conn, tid, reason='SCHEDULED_UNTIL=2099-01-01T00:00:00Z',
                                wake_check=_probe(tmp_path / 'terminal'))
        with kb.write_txn(conn):
            rid = conn.execute('SELECT id FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0]
            kb._append_event(conn, tid, 'worker_result', {'usage': {'cache_read_tokens': 10}}, run_id=rid)
        # The existing closed spending run has attributable worker usage.
        spent = budget.spend(conn, tid)
        assert spent.runs == 1 and spent.tokens['cache_read_tokens'] == 10
        with kb.write_txn(conn):
            assert not reserve(conn, tid, 'ci_run_unstartable')
            kwc._wake(conn, tid, 'ci_run_unstartable', {})
        assert budget.hold(conn, tid) is not None
        assert kb.get_task(conn, tid).status == 'triage'
        assert not _events(conn, tid, 'terminal_run_reserved')
        assert len(_events(conn, tid, 'spend_budget_decision_ready')) == 1
