"""Session-owned process discovery, without resolving foreign Hermes homes."""
from __future__ import annotations

import os
from pathlib import Path


def processes_with_hermes_home_under(roots) -> list[int]:
    """Find POSIX processes whose exec-time home is lexically under a session root.

    Resolve only our roots: resolving a foreign home touches the live profile.
    Retain both root spellings for children launched through a symlink.
    """
    if os.name != "posix":
        return []
    import psutil

    resolved = {Path(os.path.abspath(r)) for r in roots if r} | {Path(r).resolve() for r in roots if r}
    me = os.getpid()
    found = []
    for proc in psutil.process_iter():
        if proc.pid == me:
            continue
        try:
            home = proc.environ().get("HERMES_HOME")
        except (psutil.Error, OSError):
            continue
        except SystemError as exc:
            # psutil 7.2.2's Darwin procargs branch returns success with an
            # exception set (upstream #2854). Only normalize that exact fault.
            cause = exc.__cause__
            if not (str(exc) == "<built-in function proc_environ> returned a result with an exception set"
                    and (isinstance(cause, psutil.AccessDenied)
                         or (isinstance(cause, PermissionError)
                             and "sysctl(KERN_PROCARGS2) -> errno 0" in str(cause)))):
                raise
            continue
        if not home:
            continue
        try:
            text = os.fsdecode(home)
        except (OSError, ValueError):
            continue
        if not os.path.isabs(text):
            continue
        path = Path(os.path.normpath(text))
        if any(path == root or path.is_relative_to(root) for root in resolved):
            found.append(proc.pid)
    return found
