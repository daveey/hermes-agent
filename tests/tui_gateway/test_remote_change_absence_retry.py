"""A newly local destination retains refresh custody until its ACK commits."""
import pytest

from tests.tui_gateway import test_remote_change_absence as helpers
from tui_gateway import notice_fanout as nf, notice_fanout_state as state
from tui_gateway.unloaded_diagnostics import Admission

remote = helpers.remote
absent = helpers.absent


@pytest.mark.parametrize('failure', ['false', 'exception', 'ack'])
def test_appearance_retry_survives_until_refresh_and_ack(absent, monkeypatch, failure):
    assert not absent['run']().incomplete
    prior_memo = nf._remote_memo
    absent['available'] = True
    absent['clock'][0] += 60
    calls = []
    def refresh(*args, **kwargs):
        calls.append(1)
        if failure == 'exception':
            raise OSError('refresh unavailable')
        return failure != 'false'
    monkeypatch.setattr('hermes_state_outcome.refresh_outcomes_quietly', refresh)
    ack = state.ack_remote
    if failure == 'ack':
        monkeypatch.setattr(state, 'ack_remote', lambda rows: False)
    with Admission(absent['clock'][0], maintenance=True):
        assert absent['run']().incomplete
    assert nf._remote_memo is None
    # A failed recovery is pending work, not a cached absence subject to 60s maintenance.
    # Pending custody must also defeat a retained memo from before recovery began.
    nf._remote_memo = prior_memo
    absent['clock'][0] += 1
    assert absent['run']().incomplete
    assert len(calls) == 2
    assert nf._remote_memo is None
    monkeypatch.setattr(state, 'ack_remote', ack)
    monkeypatch.setattr('hermes_state_outcome.refresh_outcomes_quietly', lambda *a, **k: calls.append(1) or True)
    absent['clock'][0] += 1
    assert not absent['run']().incomplete
    assert len(calls) == 3
    assert absent['acks'][-1] == {'missing': frozenset({('owner/board/card', 'blocked')})}
    assert nf._remote_memo is not None
    # Successful durable ACK clears custody: neither retry nor maintenance refreshes again.
    absent['clock'][0] += 1
    absent['run']()
    absent['clock'][0] += 60
    with Admission(absent['clock'][0], maintenance=True):
        absent['run']()
    assert len(calls) == 3
    assert not nf._remote_pending


def test_unavailable_absence_recheck_retries_before_next_maintenance(absent):
    absent['run']()
    absent['unavailable'] = True
    absent['clock'][0] += 60
    with Admission(absent['clock'][0], maintenance=True):
        assert absent['run']().incomplete
    assert nf._remote_memo is None
    absent['unavailable'] = False
    absent['available'] = True
    absent['clock'][0] += 1
    assert not absent['run']().incomplete
    assert absent['remote']['calls'].count('refresh') == 1
    assert absent['opened'] == ['missing', 'missing', 'missing']
    assert not nf._remote_pending


@pytest.mark.parametrize('removed', [False, True])
def test_pending_recovery_uses_latest_snapshot(absent, removed):
    absent['run']()
    absent['available'] = True
    absent['remote']['refreshed'] = False
    absent['clock'][0] += 60
    with Admission(absent['clock'][0], maintenance=True):
        assert absent['run']().incomplete
    if removed:
        absent['remote']['cards'].clear()
    else:
        absent['remote']['cards'][0]['status'] = 'running'
    absent['remote']['refreshed'] = True
    absent['clock'][0] += 1
    assert not absent['run']().incomplete
    expected = None if removed else frozenset({('owner/board/card', 'running')})
    assert absent['acks'][-1] == {'missing': expected}
    assert not nf._remote_pending
