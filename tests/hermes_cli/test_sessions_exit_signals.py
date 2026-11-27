"""Exit-signal custody of the early sessions reader, under a signal at every line boundary.

CPython runs a Python signal handler between bytecodes of the main thread. These tests stand in a
line-level trace for that: a second signal is delivered at each line event inside the dispatcher
(one boundary per run), and the invariants are checked for every boundary.
"""
import os
import random
import signal
import sys

import pytest

from hermes_cli import sessions_early_boot as early

TERM, INT = signal.SIGTERM, signal.SIGINT


class _Run:
    """One dispatcher with recording previous handlers and a close that must never be re-entered
    or interrupted by a handler. Violations are recorded, not asserted: ``_act`` contains any
    exception raised by close, which would swallow an AssertionError."""
    def __init__(self, raising, close_raises=False):
        self.raising, self.close_raises = raising, close_raises
        self.log, self.closing, self.inside, self.armed = [], False, False, False
        self.violations = []
        handlers: dict = {TERM: self.previous, INT: self.previous}
        self.signals = early._ExitSignals(handlers, self.close)

    def close(self):
        if self.closing:
            self.violations.append('close re-entered')
        if self.inside:
            self.violations.append('close run inside a previous handler')
        self.closing = True
        self.log.append('close')
        self.closing = False
        if self.close_raises:
            raise TimeoutError('custody retained; close() again')

    def previous(self, signum, frame):
        if self.closing:
            self.violations.append('handler ran inside a close')
        if self.inside:
            self.violations.append('handler ran inside another handler')
        self.inside = True
        self.log.append(signal.Signals(signum).name)
        self.inside = False
        if self.raising:
            raise SystemExit(signum)


_CODES = {fn.__code__ for fn in vars(early._ExitSignals).values() if callable(fn) and hasattr(fn, '__code__')}
_CODES |= {early._ExitSignals.deferred.__wrapped__.__code__}  # type: ignore[attr-defined]
_CODES |= {_Run.close.__code__, _Run.previous.__code__}  # also inside a close and a previous handler


def _inject_at(boundary, run, scenario, granularity='line'):
    """Run ``scenario``, delivering SIGINT at event number ``boundary`` inside the dispatcher, with
    the interrupted frame, as CPython passes it. ``granularity`` 'line' counts line events (each
    handler's first line included, so the window before an outer handler queued itself is covered);
    'opcode' counts every bytecode, which is a superset of CPython's eval-breaker points (it also
    covers the instructions after a function's last line, e.g. POP_TOP/RETURN_VALUE/RERAISE).
    Returns (whether it was injected, the exception that escaped, if any)."""
    seen, injected = [0], [False]

    def trace(frame, event, arg):
        if frame.f_code not in _CODES:
            return None
        if granularity == 'opcode':
            frame.f_trace_opcodes = True
        if event == granularity and not injected[0] and run.armed:
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


@pytest.mark.parametrize('granularity', ['call', 'line', 'opcode'])
@pytest.mark.parametrize('make', [_held_term_then_boundary, _term_at_depth_zero], ids=['held', 'depth-zero'])
@pytest.mark.parametrize('close_raises', [False, True], ids=['close-ok', 'close-keeps-custody'])
@pytest.mark.parametrize('raising', [False, True], ids=['handlers-return', 'handlers-exit'])
def test_a_second_signal_at_any_boundary_is_delivered_once_after_the_first(make, close_raises, raising,
                                                                            granularity):
    boundaries = 0
    for boundary in range(1, 5000):
        run = _Run(raising, close_raises)
        injected, escaped = _inject_at(boundary, run, make(run), granularity)
        if not injected:
            break
        boundaries += 1
        # Both delivered, each once, in invocation order, each after its own close; nothing queued.
        assert run.violations == [], (boundary, run.violations)
        assert run.log == ['close', 'SIGTERM', 'close', 'SIGINT'], (boundary, run.log)
        assert run.signals.pending == [] and run.signals.depth == 0, boundary
        if raising:  # the later exit propagates (the earlier one is its context)
            assert escaped is not None and escaped.code == INT, boundary
        else:  # a close that kept custody never leaks into the interrupted code
            assert escaped is None, boundary
    assert boundaries >= (5 if granularity == 'call' else 10)  # the trace really walked the dispatcher


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


# Real CPython signal delivery. os.kill(self) runs the Python handler synchronously (PyErr_CheckSignals)
# with the interrupted frames above it, as at an eval-breaker point; a trace event only picks the moment.
# sys.call_tracing keeps the injected handler traced, so a later signal can land inside it (nesting).
_REAL = (TERM, INT, signal.SIGUSR1, signal.SIGUSR2)
_INJECT = (INT, signal.SIGUSR1, signal.SIGUSR2)


@pytest.fixture(autouse=True)
def _quiet_logging():
    import logging
    logging.disable(logging.CRITICAL)  # the contained close failure logs a traceback per delivery
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def real_handlers():
    saved = {s: signal.getsignal(s) for s in _REAL}
    yield
    for s, handler in saved.items():
        signal.signal(s, handler)


def _real_run(raising, close_raises, held, points):
    """One scenario with real signals: SIGTERM held in a custody step (``held``) or acted on at once,
    then one real signal at each event number in ``points`` (call/line/opcode/return/exception events
    in the dispatcher, its close and the previous handler). Returns (violations, events seen)."""
    run = _Run(raising, close_raises)
    for s in _REAL:
        run.signals.previous[s] = run.previous
        signal.signal(s, run.signals.handler)
    codes = _CODES
    handler_code = early._ExitSignals.handler.__code__
    count, invoked, sent, todo = [0], [], [0], list(points)
    pid = os.getpid()

    def trace(frame, event, arg):
        if frame.f_code not in codes:
            return None
        frame.f_trace_opcodes = True
        if event == 'call' and frame.f_code is handler_code:
            invoked.append(frame.f_locals['signum'])  # CPython invocation order
        if run.armed and todo:
            count[0] += 1
            if count[0] == todo[0]:
                todo.pop(0)
                signum = _INJECT[sent[0]]
                sent[0] += 1
                mark = len(invoked)
                try:
                    sys.call_tracing(os.kill, (pid, signum))
                finally:
                    if signum not in invoked[mark:]:  # 3.11 does not trace the nested handler
                        invoked.insert(mark, signum)
        return trace

    sys.settrace(trace)
    escaped = None
    try:
        if held:
            with run.signals.deferred():
                os.kill(pid, TERM)
                run.armed = True
        else:
            run.armed = True
            os.kill(pid, TERM)
    except SystemExit as error:
        escaped = error
    finally:
        sys.settrace(None)
    expected = [step for s in invoked for step in ('close', signal.Signals(s).name)]
    violations = list(run.violations)
    if run.log != expected:
        violations.append(f'log {run.log} != {expected}')
    if run.signals.pending or run.signals.depth:
        violations.append(f'pending={run.signals.pending} depth={run.signals.depth}')
    if raising and (escaped is None or escaped.code != invoked[-1]):
        violations.append(f'escaped {escaped and escaped.code!r}, want {invoked[-1]}')
    if not raising and escaped is not None:
        violations.append(f'leaked {escaped!r}')
    return violations, count[0]


def test_real_signal_while_the_delivered_handler_frame_returns_is_not_delivered_again(real_handlers):
    """Review of 1dca4a9981: SIGINT handled after the SIGTERM invocation dropped its delivered entry,
    while its frame was still on the stack, queued SIGTERM again (previous handler and close twice)."""
    run = _Run(raising=False)
    signal.signal(TERM, run.signals.handler)
    signal.signal(INT, run.signals.handler)
    fired = []

    def trace(frame, event, arg):
        if frame.f_code is early._ExitSignals.handler.__code__:
            if event == 'return' and not fired and frame.f_locals['signum'] == TERM:
                fired.append(True)
                os.kill(os.getpid(), INT)  # the SIGTERM handler frame is still on the stack
            return trace
        return None
    sys.settrace(trace)
    try:
        os.kill(os.getpid(), TERM)
    finally:
        sys.settrace(None)
    assert fired and run.violations == []
    assert run.log == ['close', 'SIGTERM', 'close', 'SIGINT']
    assert run.signals._queue == {} and run.signals.depth == 0


@pytest.mark.parametrize('held', [True, False], ids=['held', 'depth-zero'])
@pytest.mark.parametrize('close_raises', [False, True], ids=['close-ok', 'close-keeps-custody'])
@pytest.mark.parametrize('raising', [False, True], ids=['handlers-return', 'handlers-exit'])
def test_real_nested_signals_are_each_delivered_once_in_invocation_order(real_handlers, raising,
                                                                         close_raises, held):
    """One real signal at every event, then (sampled) a second one at a later event of that run,
    including inside the handler the first one ran (a nested invocation)."""
    rng = random.Random(4647)
    _, total = _real_run(raising, close_raises, held, [10 ** 9])
    assert total >= 20
    failures, runs = [], 0
    for first in range(1, total + 1):
        violations, after = _real_run(raising, close_raises, held, [first, 10 ** 9])
        runs += 1
        if violations:
            failures.append(([first], violations))
        if after > first:
            plan = [first, rng.randint(first + 1, after)]
            violations, _ = _real_run(raising, close_raises, held, plan)
            runs += 1
            if violations:
                failures.append((plan, violations))
    assert failures == [], (runs, failures[:3])
