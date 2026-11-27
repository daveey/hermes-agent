"""Disjoint CPU stages, bounded outcomes and lexical failure custody."""
import time

import pytest

from hermes_cli import loop_accounting as la
from tests.tui_gateway import test_remote_change_diagnostics as helpers
from tui_gateway import notice_fanout as nf, unloaded_diagnostics as diag

remote = helpers.remote


def burn():
    start = time.thread_time()
    while time.thread_time() - start < 0.02:
        pass


def test_stage_cpu_partition_real_card_change(remote, monkeypatch):
    remote['cards'] = [dict(board='board', id='card', key='owner/board/card', status='blocked', session='chat')]
    def refresh(*args, **kwargs):
        burn()
        return True
    monkeypatch.setattr('hermes_state_outcome.refresh_outcomes_quietly', refresh)
    # Use the real outer card-change entry with the remote fixture opener.
    from contextlib import contextmanager
    @contextmanager
    def opener(chat):
        yield remote['db'], chat
    with la.pass_('unloaded.card_change_pass'):
        nf.card_change_pass(session_db=opener, slugs=[])
    rows = la.counters()
    stages = {key: row for key, row in rows.items() if key.startswith('unloaded.diag.card_stage.')}
    assert stages
    assert all(key.rsplit('.', 1)[1] == 'complete' for key in stages)
    assert any('.lookup.' in key for key in stages)
    assert any('.outcome_refresh.' in key for key in stages)
    cpu = sum(row['cpu_s'] for row in stages.values())
    assert cpu == pytest.approx(rows['unloaded.card_change_pass']['cpu_s'], rel=0.05)


@pytest.mark.parametrize('failure', [None, ValueError, KeyboardInterrupt])
def test_stage_one_terminal_outcome_and_unreached_exclusion(failure):
    la.reset()
    if failure is None:
        with diag.Stage('lookup'):
            burn()
    else:
        with pytest.raises(failure), diag.Stage('lookup'):
            raise failure('stage failure')
    rows = {key: row for key, row in la.counters().items() if key.startswith('unloaded.diag.card_stage.')}
    assert list(rows) == ['unloaded.diag.card_stage.lookup.' + ('complete' if failure is None else 'exception')]
    assert sum(row['runs'] for row in rows.values()) == 1


@pytest.mark.parametrize('failure', [ValueError, KeyboardInterrupt])
def test_real_projection_stage_exception_custody(remote, monkeypatch, failure):
    from hermes_cli import kanban_session_activity as activity
    def fail(*args, **kwargs):
        raise failure('projection')
    monkeypatch.setattr(activity, '_remote_cards', fail)
    with pytest.raises(failure):
        remote['run']()
    rows = la.counters()
    assert rows['unloaded.diag.card_stage.projection.exception']['runs'] == 1
    assert rows['unloaded.diag.card_stage.remote_flow.exception']['runs'] == 1
    assert not any('.lookup.' in key or '.outcome_refresh.' in key for key in rows)


def test_stage_labels_bounded_and_parent_resumes_after_failure():
    la.reset()
    with diag.Stage('dispatch'):
        burn()
        with pytest.raises(ValueError), diag.Stage('private-chat-id'):
            burn()
            raise ValueError('failure')
        burn()
    rows = la.counters()
    assert set(rows) == {'unloaded.diag.card_stage.dispatch.complete', 'unloaded.diag.card_stage.unknown.exception'}
    assert rows['unloaded.diag.card_stage.dispatch.complete']['cpu_s'] >= 0.035
