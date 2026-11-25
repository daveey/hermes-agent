"""Pass cancellation crosses each best-effort decision-maintenance boundary."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch_quota as quota
from hermes_cli import kanban_decision_maintenance as maintenance
from hermes_cli import kanban_dispatch_storage as storage
from hermes_cli import kanban_scope_renewal_alarm as alarm
from tests.hermes_cli import test_kanban_hold_failure_isolation as helpers

kanban_home = helpers.kanban_home


@pytest.mark.parametrize('operation', ['renewal', 'quota'])
@pytest.mark.parametrize('cancelled', [True, False])
def test_decision_maintenance_propagates_pass_cancellation(kanban_home, monkeypatch, caplog, operation, cancelled):
    with kbc.connect_closing() as conn:
        tid = helpers._held(conn, 'runs')
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'scope_renewed', {})

        def cancel(*args, **kwargs):
            if cancelled:
                raise storage.DispatchWriteContended(storage.REASON)
            raise RuntimeError('local maintenance fault')

        if operation == 'renewal':
            monkeypatch.setattr(alarm, '_file', cancel)
            tick = lambda: alarm.sweep(conn)
        else:
            from hermes_cli.kanban_db_dispatch import DispatchResult
            monkeypatch.setattr(quota, 'release_recovered_quota_blocks', cancel)
            tick = lambda: maintenance.run(conn, DispatchResult(), board='default', dry_run=True)
        if cancelled:
            with pytest.raises(storage.DispatchWriteContended):
                tick()
            assert not caplog.records
        else:
            tick()
            assert len(caplog.records) == 1
            assert caplog.records[0].exc_info is not None
