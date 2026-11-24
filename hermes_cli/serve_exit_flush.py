"""Signal-time flush without importing the agent stack just to install a handler."""
import contextlib
import os
import signal
import sys
import threading

_previous = {}


def dispatch(signum, frame, previous):
    if previous == signal.SIG_IGN:
        return
    gateway = sys.modules.get('tui_gateway.server')
    # sys.modules registration precedes execution. Never touch partially bound
    # method_ctx globals, or cause a signal-time import of the agent/provider tree.
    if gateway is not None and not getattr(getattr(gateway, '__spec__', None), '_initializing', False):
        for name in ('_prepare_delegations_before_exit', '_flush_sessions_before_exit',
                     '_stop_turns_before_exit', '_journal_background_jobs_before_exit'):
            action = getattr(gateway, name, None)
            if action is not None:
                with contextlib.suppress(Exception):
                    action()
    if callable(previous):
        previous(signum, frame)
    else:
        try:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        except (OSError, ValueError):
            raise SystemExit(128 + int(signum)) from None


def _handle(signum, frame):
    dispatch(signum, frame, _previous[signum])


def install():
    if threading.current_thread() is not threading.main_thread():
        return False
    installed = False
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            if signal.getsignal(signum) is not _handle:
                _previous[signum] = signal.signal(signum, _handle)
            installed = True
        except (OSError, ValueError):
            continue
    return installed
