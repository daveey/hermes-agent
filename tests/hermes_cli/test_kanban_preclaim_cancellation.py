"""Preclaim best-effort services must not swallow pass-wide cancellation."""
from contextlib import closing
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_dispatch_storage as storage
from hermes_cli import kanban_wake_check as wake
from hermes_cli.kanban_db_dispatch import DispatchResult
from tests.hermes_cli import test_kanban_hold_failure_isolation as helpers

kanban_home = helpers.kanban_home


def test_wake_tick_unwinds_on_real_writer_contention(kanban_home, caplog):
    with kbc.connect_closing() as conn:
        tid = helpers._held(conn, 'runs')
        # A wake check on a held card is stale: the real tick must remove it.
        with kb.write_txn(conn):
            conn.execute('INSERT INTO task_wake_checks(task_id, cmd, every, next_at) VALUES (?, ?, ?, ?)',
                         (tid, 'true', 120, 0))
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        after = []

        def tick():
            wake.run_due_wake_checks(conn, now=1000)
            after.append('continued')
            return DispatchResult()

        with closing(sqlite3.connect(path, isolation_level=None)) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            try:
                result = storage.run(conn, tick, board='default')
                assert result.storage_skip_reason == storage.REASON
                assert after == []
                assert conn.execute('SELECT count(*) FROM task_wake_checks').fetchone()[0] == 1
                warnings = [r for r in caplog.records if r.levelname == 'WARNING']
                assert len(warnings) == 1
                assert warnings[0].exc_info is None
            finally:
                blocker.rollback()
        caplog.clear()
        assert storage.run(conn, tick, board='default').storage_skip_reason is None
        assert after == ['continued']
        assert conn.execute('SELECT count(*) FROM task_wake_checks').fetchone()[0] == 0
        assert not [r for r in caplog.records if r.levelname == 'WARNING']


@pytest.mark.parametrize('boundary', ['supervisor', 'placement', 'owner_backfill', 'spend_reviews'])
@pytest.mark.parametrize('cancel', [True, False])
def test_preclaim_boundaries_distinguish_cancellation(kanban_home, monkeypatch, caplog, boundary, cancel):
    from hermes_cli import kanban_supervisor_requests as supervisor
    from hermes_cli import kanban_supervisor_gate as gate
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_dispatch_maintenance as maintenance
    from hermes_cli import kanban_worker_placement as placement
    from hermes_cli import kanban_gateway_drain as drain
    from hermes_cli import kanban_owner_chat as owner
    from hermes_cli import kanban_spend_reviewer as reviews

    error = storage.DispatchWriteContended(storage.REASON) if cancel else ValueError('local service failure')

    def fail(*args, **kwargs):
        raise error

    with kbc.connect_closing() as conn:
        if boundary == 'supervisor':
            monkeypatch.setattr(supervisor, '_non_worker_refusal', lambda: None)
            monkeypatch.setattr(gate, 'dispatch', fail)
            call = lambda: supervisor.dispatcher_drain(conn)
        elif boundary == 'owner_backfill':
            monkeypatch.setattr(owner, 'maybe_backfill', fail)
            call = lambda: maintenance._own_ownerless_cards(conn)
        elif boundary == 'spend_reviews':
            monkeypatch.setattr(reviews, 'start_due', fail)
            call = lambda: maintenance._start_spend_reviews(conn)
        else:
            monkeypatch.setattr(dispatch, '_run_reclaim_phase', lambda *a, **kw: None)
            monkeypatch.setattr(maintenance, 'sweep', lambda *a, **kw: None)
            monkeypatch.setattr(drain, 'dispatch_paused', lambda: False)
            monkeypatch.setattr(placement, 'dispatch_tick', fail)
            # Stop normal continuation before any worker pickup or execution.
            monkeypatch.setattr(drain, 'core_update_hold', lambda: 'test hold')
            from hermes_cli import kanban_candidate_receipts
            monkeypatch.setattr(kanban_candidate_receipts, 'global_hold', lambda *a: None)
            call = lambda: dispatch._dispatch_once_locked(conn)
        if cancel:
            with pytest.raises(storage.DispatchWriteContended):
                call()
            assert not [r for r in caplog.records if r.exc_info]
        else:
            call()
            assert any(r.exc_info for r in caplog.records)
