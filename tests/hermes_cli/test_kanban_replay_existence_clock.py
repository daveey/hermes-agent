"""Current-row recovery uses bounded clock-owned retry deadlines."""
import importlib
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_replay_existence as existence
from hermes_cli import kanban_replay_journal as journal
from tests.hermes_cli import test_kanban_replay_sender as helpers
from tests.hermes_cli import test_kanban_replay_current_row as current
from tests.hermes_cli import test_kanban_replay_existence as cases
from tests.hermes_cli.test_kanban_replay_http_recovery import clocks

live = helpers.live


@contextmanager
def current_clocks(monkeypatch):
    try:
        with clocks(monkeypatch) as clock:
            importlib.reload(existence)
            yield clock
    finally:
        importlib.reload(existence)


def fail_read(*args):
    raise OSError('current service unavailable')


@pytest.mark.parametrize('jump,elapsed,progress', [(-3600, 61, True), (3600, 1, False), (0, 2, True)])
def test_wall_jump_does_not_extend_or_skip_retry(live, monkeypatch, jump, elapsed, progress):
    _, receiver = current.issued_parent(live)
    frozen = journal.pending_wire(live)
    with current_clocks(monkeypatch) as clock:
        original = receiver.row_evidence
        receiver.row_evidence = fail_read
        cases.hold(live, receiver)
        assert existence.fault(live)['attempts'] == 1
        receiver.row_evidence = original
        before = list(receiver.calls)
        clock['wall'] += jump + elapsed
        clock['mono'] += elapsed
        cases.hold(live, receiver)
        assert (receiver.calls != before) is progress
        if progress:
            assert journal.pending_wire(live) is None
            assert existence.fault(live) is None
            assert len(receiver.sent) == 1
        else:
            assert journal.pending_wire(live) == frozen
            assert existence.fault(live)['attempts'] == 1


@pytest.mark.parametrize('legacy', [False, True])
def test_foreign_clock_reanchors_once_without_resetting_attempts(live, monkeypatch, legacy):
    _, receiver = current.issued_parent(live)
    frozen = journal.pending_wire(live)
    with current_clocks(monkeypatch) as clock:
        receiver.row_evidence = fail_read
        cases.hold(live, receiver)
        if legacy:
            with kbc.write_txn(live):
                if 'retry_clock' in {row[1] for row in live.execute('PRAGMA table_info(kanban_replay_existence_fault1)')}:
                    live.execute('ALTER TABLE kanban_replay_existence_fault1 DROP COLUMN retry_clock')
                live.execute('UPDATE kanban_replay_existence_fault1 SET retry_after=99999999')
        importlib.reload(existence)
        clock['mono'] = 10.0
        reopened = helpers.reopen(live)
        receiver.conn = reopened
        try:
            before = list(receiver.calls)
            cases.hold(reopened, receiver)
            held = existence.fault(reopened)
            assert held['retry_after'] == 12.0
            assert held['attempts'] == 1 and not held['paused']
            assert receiver.calls == before and journal.pending_wire(reopened) == frozen
            cases.hold(reopened, receiver)
            assert existence.fault(reopened) == held, 'same clock must not renew its own deadline'
            clock['mono'] = 12.0
            cases.hold(reopened, receiver)
            assert existence.fault(reopened)['attempts'] == 2
            assert journal.pending_wire(reopened) == frozen
        finally:
            reopened.close()


def test_paused_fault_is_not_rearmed_by_foreign_clock(live, monkeypatch):
    _, receiver = current.issued_parent(live)
    receiver.evidence.update(state='unknown', row=None)
    with current_clocks(monkeypatch):
        cases.hold(live, receiver)
        before = existence.fault(live)
        calls = list(receiver.calls)
        importlib.reload(existence)
        cases.hold(live, receiver)
        assert existence.fault(live) == before and before['paused'] == 1
        assert receiver.calls == calls
