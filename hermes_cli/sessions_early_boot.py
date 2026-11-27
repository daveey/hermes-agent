"""Pre-import native capture custody, after producer admission, before app assembly."""
from contextlib import contextmanager
import os
import signal
import sys
import threading


class EarlySessions:
    """One owner across CLI import, app startup, and failed shutdown."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.adopted = False
        self.closed = False
        # The app lifespan closes on a worker thread (asyncio.to_thread); an exit signal closes on
        # the main thread. The runtime's close is not safe to run twice at once.
        self._close_lock = threading.Lock()

    def adopt(self):
        if self.adopted or self.closed:
            raise RuntimeError('early sessions custody already consumed')
        self.adopted = True
        return self.runtime

    def close(self, *, blocking=True):
        """Close once. With ``blocking=False`` (the signal path) a close already running on another
        thread is left to finish and False is returned; it never waits inside a signal handler."""
        if not self._close_lock.acquire(blocking):
            return False
        try:
            if not self.closed:
                self.runtime.close()
                self.closed = True  # failed drain retains custody for the outer retry
            return True
        finally:
            self._close_lock.release()


def adopted_early_owner(state):
    """Startup policy only, independent of capture readiness or environment flags."""
    owner = getattr(state, 'early_sessions_owner', None)
    return (isinstance(owner, EarlySessions) and owner.adopted and not owner.closed
            and getattr(state, 'sessions_facts', None) is owner.runtime)


_EXIT_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class _ExitSignals:
    """SIGTERM/SIGINT custody for the early sessions reader (main thread only, where CPython runs
    Python signal handlers).

    A signal is never acted on inside a main-thread custody step: a spawn whose child ``prepare()``
    alone owns, or a close (the reader's, or the runtime's, whose locks are not re-entrant). An
    *invocation* is one call of ``handler`` (its frame); an *action* for a signum is ``close()`` then
    the previous handler. Every invocation is one entry of an ordered queue (``_queue``: handler
    frame -> [signum, flags]). Invariants:
      I1 queued once: an invocation is queued by ONE ``dict.setdefault`` keyed by its frame (a C
         call, so no handler runs inside it), done by the invocation itself or by the first handler
         that interrupts it (at any bytecode, even before its first line) and finds it on the stack.
         The walk adds only *unfinished* invocations: ``finished`` is set first thing in the
         ``finally``, after the invocation is queued, and an entry is dropped only when it is
         delivered AND its invocation has reached that ``finally``. So a walk that still finds the
         frame either finds its entry or runs before its own setdefault; nobody queues it again
         after its tombstone is dropped, though its frame stays on the stack until it returns.
      I2 exactly once, in invocation order: outer invocations are queued oldest first, ahead of the
         interrupting one; an entry is marked delivered before its action runs. Caveat: an
         undelivered entry for the signum being delivered collapses into that delivery (a pending
         repeat), which can move it ahead of other signals but never loses it.
      I3 one dispatcher: actions run only from ``depth`` 0, and the dispatcher holds ``depth`` above
         0 while it runs, so a signal arriving inside it, its close or a previous handler is only
         queued. Nested ``depth`` changes are balanced, so its read-modify-write is safe.
      I4 nothing stranded: whoever brings ``depth`` back to 0 reads the queue AFTER the decrement.
      I5 exceptions: later deliveries still run when an earlier action raised; the last exception
         propagates, chained. A close failure never propagates (below).
      C1 custody is main-thread only: ``deferred()`` covers ``prepare()`` and the early_sessions
         close. A close on another thread (the app lifespan's ``asyncio.to_thread``) is guarded by
         ``EarlySessions``' close lock, which the signal path only tries: if it is held, the close is
         left to that thread and the previous handler still runs.
    Residue: an invocation unwound by an exception before or inside its ``finally`` leaves its
    delivered entry as a tombstone (the frame is dead but referenced, so the key is never reused);
    an empty queue is therefore not guaranteed once an exit propagates.
    Order is CPython's handler-invocation order (it runs tripped handlers in signal-number order,
    not kernel arrival order). A thread signal mask cannot do this: a process-directed signal caught
    on another thread still runs its Python handler on the main thread.

    A close that keeps custody (raises) is logged and the previous handler still runs: a returning
    one resumes the interrupted code, which must not see the close's exception; the next close
    retries. Contract: a previous handler must return or raise; one that blocks keeps later signals
    queued (exit escalation is then the supervisor's SIGKILL).
    """
    _DELIVERED, _RETURNED = 1, 2

    def __init__(self, previous, close):
        self.previous, self._close = previous, close
        self.depth = 0
        self._queue = {}  # handler frame -> [signum, flags], invocation order

    @property
    def pending(self):
        return [entry[0] for entry in self._queue.values() if not entry[1] & self._DELIVERED]

    def handler(self, signum, frame):
        me = sys._getframe()
        try:
            outer, walk = [], frame
            while walk is not None:  # outer handler invocations this one interrupted
                if (walk.f_code is _HANDLER_CODE and walk.f_locals.get('self') is self
                        and not walk.f_locals.get('finished')):
                    outer.append(walk)
                walk = walk.f_back
            for invocation in reversed(outer):  # outermost (oldest) first
                self._queue.setdefault(invocation, [invocation.f_locals['signum'], 0])
            self._queue.setdefault(me, [signum, 0])
            if self.depth == 0:
                self._dispatch()
        finally:
            # From here this invocation is queued (by its own setdefault, or by the handler that
            # interrupted it first) and is never queued again. Its frame stays on the stack until it
            # returns, after its tombstone is dropped here or by a dispatcher: a handler
            # interrupting it then must not re-queue it (the walk above skips finished invocations).
            finished = True  # noqa: F841  # health: allow F841 -- read via f_locals by a nested handler's walk
            entry = self._queue.get(me)
            if entry is not None:
                entry[1] |= self._RETURNED
                if entry[1] & self._DELIVERED:
                    self._queue.pop(me, None)

    @contextmanager
    def deferred(self):
        self.depth += 1
        try:
            yield
        finally:
            self._leave()

    def _leave(self):
        self.depth -= 1
        if self.depth == 0 and self._next() is not None:
            self._dispatch()

    def _dispatch(self):
        self.depth += 1
        try:
            self._deliver_queue()
        finally:
            self._leave()

    def _next(self):
        for key, entry in list(self._queue.items()):
            if not entry[1] & self._DELIVERED:
                return key, entry
        return None

    def _deliver_queue(self):
        found = self._next()  # depth > 0: a handler arriving now only queues
        if found is None:
            return
        key, entry = found
        signum = entry[0]
        for other_key, other in list(self._queue.items()):  # one delivery per pending signal
            if other[0] == signum and not other[1] & self._DELIVERED:
                other[1] |= self._DELIVERED
                if other[1] & self._RETURNED:
                    self._queue.pop(other_key, None)
        try:
            self._act(signum, key)
        finally:
            self._deliver_queue()

    def _act(self, signum, frame):
        handler = self.previous.get(signum, signal.SIG_DFL)
        if handler == signal.SIG_IGN:
            return
        try:
            self._close()
        except Exception:  # health: allow BLE001 -- signal boundary: any close failure keeps custody (next close retries); the previous handler must still run
            import logging
            logging.getLogger(__name__).warning(
                'sessions capture close on signal %s kept custody', signum, exc_info=True)
        if callable(handler):
            handler(signum, frame)
        else:
            raise SystemExit(128 + signum)


_HANDLER_CODE = _ExitSignals.handler.__code__


@contextmanager
def early_sessions(*, headless, isolated):
    # Preserve legacy/embedded/non-Linux ordering and the supported rollback.
    # health: allow HX002 -- creator-defined startup switch, not a credential
    enabled = os.environ.get('HERMES_FACTS_SESSIONS', '1').strip().lower() not in ('0', 'off', 'false', 'no')
    if not headless or not enabled or not sys.platform.startswith('linux'):
        yield None
        return
    from hermes_cli.proof_home import assert_backend_home
    from hermes_cli.update_proxy_env import restore_user_proxy_env
    from hermes_cli.isolated_scope import record_launch_scope
    from hermes_cli.process_identity import is_desktop_owned_backend

    assert_backend_home(isolated=isolated)
    restore_user_proxy_env()
    record_launch_scope(isolated=isolated, desktop_owned=is_desktop_owned_backend())
    # The capture child is spawned HERE, before the imports below, so its own imports run beside
    # them. From this line one try/finally owns it: the reader until the runtime owns the reader.
    from hermes_cli.sessions_capture_process import CaptureProcess
    reader = CaptureProcess()
    owner = None
    previous = {}

    def close_on_signal():
        if owner is None:
            reader.close()  # only the main thread holds the reader before the runtime owns it
        elif not owner.close(blocking=False):
            import logging
            logging.getLogger(__name__).info(
                'sessions capture close already running on another thread; exit signal chains on')

    signals = _ExitSignals(previous, close_on_signal)
    stop = signals.handler

    def close():
        with signals.deferred():
            (reader if owner is None else owner).close()

    try:
        # signal.signal is illegal off the main thread (serve_update_wait and
        # other cmd_dashboard harnesses). CPython delivers signals to the main
        # thread anyway, so skip rather than abort capture.
        if threading.current_thread() is threading.main_thread():
            for signum in _EXIT_SIGNALS:
                try:
                    # Record the previous handler BEFORE installing ours: a signal handled right
                    # after the install must find it.
                    previous[signum] = signal.getsignal(signum)
                    signal.signal(signum, stop)
                except (ValueError, OSError, RuntimeError):
                    previous.pop(signum, None)
        with signals.deferred():
            reader.prepare()
        from hermes_cli.sessions_native_startup import SessionsRuntime
        from hermes_cli.web_routers import profiles

        owner = EarlySessions(SessionsRuntime(profiles, profiles._profile_targets('sessions facts startup'),
                                              capture_reader=reader))
        owner.runtime.start_background()
        yield owner
    finally:
        try:
            close()
        finally:
            if previous and threading.current_thread() is threading.main_thread():
                for signum, handler in previous.items():
                    # Do not remove a later installed exit-flush handler.
                    try:
                        if signal.getsignal(signum) is stop:
                            signal.signal(signum, handler)
                    except (ValueError, OSError, RuntimeError):
                        pass
