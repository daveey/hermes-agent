"""Identity-retaining custody for the disposable completion descendant."""

import psutil


def descendant_exited(proc):
    try:
        return not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def reap_descendant(proc):
    # kill() checks the retained creation time before signalling: never rebuild
    # a Process from a PID after cancellation, when that PID may be reused.
    try:
        proc.kill()
    except psutil.NoSuchProcess:
        pass
