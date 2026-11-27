"""Exit-signal custody of the early sessions reader, under a signal at every line boundary.

CPython runs a Python signal handler between bytecodes of the main thread. These tests stand in a
line-level trace for that: a second signal is delivered at each line event inside the dispatcher
(one boundary per run), and the invariants are checked for every boundary.
"""
import signal
import sys

import pytest

from hermes_cli import sessions_early_boot as early

TERM, INT = signal.SIGTERM, signal.SIGINT


class _Run:
    """One dispatcher with recording previous handlers and a close that must never be re-entered
    or interrupted by a handler."""
    def __init__(self, raising, close_raises=False):
        self.raising, self.close_raises = raising, close_raises
        self.log, self.closing, self.inside, self.armed = [], False, False, False
        handlers: dict = {TERM: self.previous, INT: self.previous}
        self.signals = early._ExitSignals(handlers, self.close)

    def close(self):
        assert not self.closing, 'close re-entered'
        assert not self.inside, 'close run inside a previous handler'
        self.closing = True
        self.log.append('close')
        self.closing = False
        if self.close_raises:
            raise TimeoutError('custody retained; close() again')

    def previous(self, signum, frame):
        assert not self.closing, 'handler ran inside a close'
        assert not self.inside, 'handler ran inside another handler'
        self.inside = True
        self.log.append(signal.Signals(signum).name)
        self.inside = False
        if self.raising:
            raise SystemExit(signum)


_CODES = {fn.__code__ for fn in vars(early._ExitSignals).values() if callable(fn) and hasattr(fn, '__code__')}
_CODES |= {early._ExitSignals.deferred.__wrapped__.__code__}  # type: ignore[attr-defined]
_CODES |= {_Run.close.__code__, _Run.previous.__code__}  # also inside a close and a previous handler


def _inject_at(boundary, run, scenario):
    """Run ``scenario``, delivering SIGINT at line event number ``boundary`` inside the dispatcher,
    with the interrupted frame, as CPython passes it. Line events include each handler's first
    line, so the window before an outer handler queued itself is covered.
    Returns (whether it was injected, the exception that escaped, if any)."""
    seen, injected = [0], [False]

    def trace(frame, event, arg):
        if frame.f_code not in _CODES:
            return None
        if event == 'line' and not injected[0] and run.armed:
            seen[0] += 1
            if seen[0] == boundary:
                injected[0] = True
                run.signals.handler(INT, frame)
        return trace
    sys.settrace(trace)
    try:
        scenario()
        escaped = None
    except SystemExit as error:
        escaped = error
    finally:
        sys.settrace(None)
    return injected[0], escaped


def _held_term_then_boundary(run):
    def scenario():
        with run.signals.deferred():          # a custody step (prepare, close)
            run.signals.handler(TERM, sys._getframe())   # SIGTERM arrives inside it: queued
            run.armed = True
    return scenario


def _term_at_depth_zero(run):
    def scenario():
        run.armed = True
        run.signals.handler(TERM, sys._getframe())  # nothing held: SIGTERM is acted on at once
    return scenario


@pytest.mark.parametrize('make', [_held_term_then_boundary, _term_at_depth_zero], ids=['held', 'depth-zero'])
@pytest.mark.parametrize('close_raises', [False, True], ids=['close-ok', 'close-keeps-custody'])
@pytest.mark.parametrize('raising', [False, True], ids=['handlers-return', 'handlers-exit'])
def test_a_second_signal_at_any_boundary_is_delivered_once_after_the_first(make, close_raises, raising):
    boundaries = 0
    for boundary in range(1, 500):
        run = _Run(raising, close_raises)
        injected, escaped = _inject_at(boundary, run, make(run))
        if not injected:
            break
        boundaries += 1
        # Both delivered, each once, in invocation order, each after its own close; nothing queued.
        assert run.log == ['close', 'SIGTERM', 'close', 'SIGINT'], (boundary, run.log)
        assert run.signals.pending == [] and run.signals.depth == 0, boundary
        if raising:  # the later exit propagates (the earlier one is its context)
            assert escaped is not None and escaped.code == INT, boundary
        else:  # a close that kept custody never leaks into the interrupted code
            assert escaped is None, boundary
    assert boundaries >= 10  # the trace really walked the dispatcher


def test_a_signal_while_none_is_held_acts_at_once():
    run = _Run(raising=False)
    run.signals.handler(TERM, None)
    assert run.log == ['close', 'SIGTERM'] and run.signals.depth == 0 and run.signals._queue == {}


def test_a_repeat_of_a_queued_signal_collapses_and_ignored_signals_are_not_acted_on():
    run = _Run(raising=False)
    run.signals.previous[INT] = signal.SIG_IGN
    with run.signals.deferred():
        run.signals.handler(TERM, None)
        run.signals.handler(INT, None)
        run.signals.handler(TERM, None)
    assert run.log == ['close', 'SIGTERM'] and run.signals._queue == {}


def test_default_disposition_exits_with_the_shell_code_after_close():
    run = _Run(raising=False)
    run.signals.previous[TERM] = signal.SIG_DFL
    with pytest.raises(SystemExit) as exit_info:
        run.signals.handler(TERM, None)
    assert exit_info.value.code == 128 + TERM and run.log == ['close']


def test_a_close_that_keeps_custody_still_lets_the_previous_handler_run():
    run = _Run(raising=True)

    def failing():
        run.log.append('close')
        raise TimeoutError('custody retained')
    run.signals._close = failing
    with pytest.raises(SystemExit):
        run.signals.handler(TERM, None)
    assert run.log == ['close', 'SIGTERM'] and run.signals.depth == 0
