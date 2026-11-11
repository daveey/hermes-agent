"""Build diagnostics retain the failing call site without unbounded payloads."""
import logging

from tui_gateway.build_diagnostics import log_build_failure


def test_bounded_traceback_retains_innermost_frame(caplog):
    def failing_build(depth):
        local_secret = 'not-a-log-field'
        if depth:
            return failing_build(depth - 1)
        raise ValueError('failure ' + 'x' * 20000 + local_secret)
    try:
        failing_build(35)
    except ValueError as exc:
        log_build_failure(logging.getLogger('build-diagnostic-test'), 'ui-id', 'stored-id', exc)
    assert caplog.records[-1].exc_info is not None
    message = caplog.text
    assert 'ui-id (stored-id): ValueError: failure' in message
    assert 'Traceback (most recent call last;' in message
    assert message.count('in failing_build') == 20
    assert 'not-a-log-field' not in message
    assert 'raise ValueError' not in message
    assert len(message) < 8500


def test_wrapped_failure_keeps_cause_frames(caplog):
    def inner_failure():
        raise ValueError('original cause')
    try:
        try:
            inner_failure()
        except ValueError as exc:
            raise RuntimeError('outer wrapper') from exc
    except RuntimeError as exc:
        log_build_failure(logging.getLogger('build-diagnostic-test'), 'ui', 'stored', exc)
    assert 'in inner_failure' in caplog.text
    assert 'ValueError: original cause' in caplog.text
    assert 'RuntimeError: outer wrapper' in caplog.text
    assert caplog.records[-1].exc_info[2] is None
