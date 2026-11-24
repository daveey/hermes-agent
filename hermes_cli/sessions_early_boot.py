"""Pre-import native capture custody, after producer admission and plugin discovery."""
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
    from hermes_cli.sessions_native_startup import SessionsRuntime
    from hermes_cli.web_routers import profiles

    owner = EarlySessions(SessionsRuntime(profiles, profiles._profile_targets('sessions facts startup')))
    previous = {}

    def stop(signum, frame):
        handler = previous[signum]
        if handler == signal.SIG_IGN:
            return
        owner.close()
        if callable(handler):
            handler(signum, frame)
        elif handler != signal.SIG_IGN:
            raise SystemExit(128 + signum)

    try:
        # signal.signal is illegal off the main thread (serve_update_wait and
        # other cmd_dashboard harnesses). CPython delivers signals to the main
        # thread anyway, so skip rather than abort capture.
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                try:
                    previous[signum] = signal.signal(signum, stop)
                except (ValueError, OSError, RuntimeError):
                    previous.pop(signum, None)
        owner.runtime.start_background()
        yield owner
    finally:
        try:
            owner.close()
        finally:
            if previous and threading.current_thread() is threading.main_thread():
                for signum, handler in previous.items():
                    # Do not remove a later installed exit-flush handler.
                    try:
                        if signal.getsignal(signum) is stop:
                            signal.signal(signum, handler)
                    except (ValueError, OSError, RuntimeError):
                        pass
