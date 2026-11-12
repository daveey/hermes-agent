"""Signal primitives shared by worker expiry and reclaim termination."""
import signal


def sigkill(kill, pid: int) -> bool:
    """Best-effort SIGKILL; True when the signal was delivered."""
    try:
        # signal.SIGKILL doesn't exist on Windows; SIGTERM maps to TerminateProcess.
        kill(int(pid), getattr(signal, 'SIGKILL', signal.SIGTERM))
        return True
    except (ProcessLookupError, OSError):
        return False
