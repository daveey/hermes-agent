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

    def adopt(self):
        if self.adopted or self.closed:
            raise RuntimeError('early sessions custody already consumed')
        self.adopted = True
        return self.runtime

    def close(self):
        if not self.closed:
            self.runtime.close()
            self.closed = True  # failed drain retains custody for the outer retry


def adopted_early_owner(state):
    """Startup policy only, independent of capture readiness or environment flags."""
    owner = getattr(state, 'early_sessions_owner', None)
    return (isinstance(owner, EarlySessions) and owner.adopted and not owner.closed
            and getattr(state, 'sessions_facts', None) is owner.runtime)


_EXIT_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class _ExitSignals:
    """SIGTERM/SIGINT custody for the early sessions reader (main thread only, where CPython runs
    Python signal handlers).

    A signal is never acted on inside a custody step: a spawn whose child ``prepare()`` alone owns,
    or a close (the reader's, or the runtime's, whose locks are not re-entrant). Every signal goes
    through one FIFO queue, and only the dispatcher pops it, so:
      * ``handler`` always enqueues first (a repeat of a still-queued signal collapses into it, as
        a pending signal does), then dispatches only when ``depth`` is 0;
      * the dispatcher holds ``depth`` above 0 while it runs, so a signal arriving inside it, its
        close or a previous handler is queued behind, never run inside them and never ahead of
        an older one; every entry it pops is delivered even when an earlier handler raised (the
        last exception propagates, chained), as an unqueued signal would run while it unwound;
      * whoever brings ``depth`` back to 0 reads the queue AFTER the decrement and dispatches it:
        a signal queued before the decrement is seen there; one after it dispatches itself.
    A thread signal mask cannot do this: a process-directed signal caught on another thread still
    runs its Python handler on the main thread, so masking the main thread does not defer it.

    Contract: a previous handler must return or raise; one that blocks keeps later signals queued
    (exit escalation is then the supervisor's SIGKILL).
    """
    def __init__(self, previous, close):
        self.previous, self._close = previous, close
        self.depth = 0
        self.pending = []  # (signum, frame) in arrival order

    def handler(self, signum, frame):
        if self.previous[signum] == signal.SIG_IGN:
            return
        if all(signum != queued for queued, _ in self.pending):
            self.pending.append((signum, frame))
        if self.depth == 0:
            self._dispatch()

    @contextmanager
    def deferred(self):
        self.depth += 1
        try:
            yield
        finally:
            self._leave()

    def _leave(self):
        self.depth -= 1
        if self.pending and self.depth == 0:
            self._dispatch()

    def _dispatch(self):
        self.depth += 1
        try:
            self._deliver_queue()
        finally:
            self._leave()

    def _deliver_queue(self):
        if self.pending:  # depth > 0: no handler can pop or run in between
            signum, frame = self.pending.pop(0)
            try:
                self._act(signum, frame)
            finally:
                self._deliver_queue()

    def _act(self, signum, frame):
        handler = self.previous[signum]
        try:
            self._close()
        finally:  # a close that keeps custody still lets the previous handler exit
            if callable(handler):
                handler(signum, frame)
            else:
                raise SystemExit(128 + signum)


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
    signals = _ExitSignals(previous, lambda: (reader if owner is None else owner).close())
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
                    previous[signum] = signal.signal(signum, stop)
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
