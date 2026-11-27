"""Remote retry sites identify every armed occurrence, including simultaneous faults."""
import pytest

from hermes_cli import loop_accounting as accounting
from tests.tui_gateway import test_remote_change_diagnostics as helpers
from tui_gateway import notice_fanout as fanout, notice_fanout_state as state
from tui_gateway.notice_fanout_result import FanoutResult

remote = helpers.remote


@pytest.mark.parametrize('failure', ['inputs_error', 'initial_poll_failed', 'seed_failed', 'db_none',
                                    'refresh_failed', 'exception', 'ack_failed', 'simultaneous'])
def test_C_retry_site_reasons_partition_armed_retries(remote, monkeypatch, failure):
    armed = []
    original = FanoutResult.retry
    def retry(self, *args, **kwargs):
        armed.append(kwargs.get('reason', 'unknown'))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(FanoutResult, 'retry', retry)
    def fail(*args, **kwargs):
        raise OSError('injected fault')
    if failure in ('inputs_error', 'simultaneous'):
        monkeypatch.setattr(fanout, '_remote_inputs_key', fail)
    if failure == 'initial_poll_failed':
        remote['polls'][0] = None
    if failure == 'seed_failed':
        remote.update(previous=None, seed=False)
    if failure in ('db_none', 'refresh_failed', 'exception', 'simultaneous'):
        remote['cards'] = [dict(board='board', id='card', key='owner/board/card', status='blocked', session='chat')]
    if failure in ('db_none', 'simultaneous'):
        remote['db'] = None
    if failure == 'refresh_failed':
        remote['refreshed'] = False
    if failure == 'exception':
        monkeypatch.setattr(fanout, 'chat_skip_reason', fail)
    if failure == 'ack_failed':
        monkeypatch.setattr(state, 'ack_remote', lambda *args: False)
    result = remote['run']()
    expected = ['inputs_error', 'db_none'] if failure == 'simultaneous' else [
        'refresh_failed' if failure == 'ack_failed' else failure]
    assert armed == expected
    assert result.incomplete
    prefix = 'unloaded.diag.remote.retry.'
    reasons = {key[len(prefix):]: value['runs'] for key, value in accounting.counters().items() if key.startswith(prefix)}
    assert reasons == {reason: expected.count(reason) for reason in expected}
    assert sum(reasons.values()) == len(armed)
    assert result.retry_origin[1] == 'remote_card_change'
    assert result.retry_origin[2] in expected
    assert result.retry_origin[2] != 'unknown'
