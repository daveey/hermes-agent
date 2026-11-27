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
    # Signal custody. The handler never acts inside a custody step (a spawn whose child prepare()
    # alone owns, or a close: the reader's, or the runtime's, whose locks are not re-entrant). It
    # queues the signal instead, and the step that brings ``depth`` back to 0 delivers the queue.
    # A thread signal mask cannot do this: CPython runs Python handlers on the main thread whichever
    # thread the kernel picked, so a mask on the main thread alone does not defer them.
    depth = [0]
    pending = []  # (signum, frame), arrival order; a repeat of a queued signal collapses into it

    @contextmanager
    def deferred():
        depth[0] += 1
        try:
            yield
        finally:
            depth[0] -= 1
            # Read after the decrement: a signal queued before it is seen here; one arriving after
            # it acts at once (and drains the queue itself).
            if depth[0] == 0:
                drain()

    def drain():
        # Each queued signal runs even when an earlier handler raised, as an unqueued second signal
        # would while the first one's exit unwinds; the last exception propagates, chained.
        if pending and depth[0] == 0:
            signum, frame = pending.pop(0)
            try:
                act(signum, frame)
            finally:
                drain()

    def close():
        with deferred():
            (reader if owner is None else owner).close()

    def act(signum, frame):
        handler = previous[signum]
        with deferred():
            try:
                close()
            finally:  # a close that keeps custody still lets the previous handler exit
                if callable(handler):
                    handler(signum, frame)
                else:
                    raise SystemExit(128 + signum)

    def stop(signum, frame):
        if previous[signum] == signal.SIG_IGN:
            return
        if depth[0]:
            if all(signum != queued for queued, _ in pending):
                pending.append((signum, frame))
            return
        act(signum, frame)

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
        with deferred():
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
