"""POSIX cross-process update admission protocol (stdlib only).

Use an explicit installation root, not a board directory. Never unlink admission.lock.
The updater sets the hold under the lock, releases the lock, then reads reservations
BEFORE existing busy ledgers. Zero reservations alone is NOT an idle proof.
"""
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import threading
import time
import uuid

SUPPORTED = os.name == "posix"
CAPABILITY = {"version": 1, "supported": SUPPORTED, "scope": "installation-root", "lock": "core-update/admission.lock",
              "hold": "core-update/holds/hermes-sync", "state": "locked-reservations"}
HOLD_MESSAGE = "Hermes update admission is paused or busy; retry shortly (or after the update completes)."


@contextmanager
def admission_lock(home, *, timeout=10.0):
    import fcntl

    root = Path(home) / "core-update"
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / "admission.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Hermes update admission lock is busy") from None
                time.sleep(0.01)
        yield root
    finally:
        os.close(fd)


def is_held(root):
    # A dangling marker symlink is a hold; permission/I/O errors are NOT absence.
    try:
        (Path(root) / "holds" / "hermes-sync").lstat()
    except FileNotFoundError:
        return False
    return True


class Reservation:
    """An independently locked file, automatically unlocked on process exit."""
    def __init__(self, fd, path):
        self.fd = fd
        self.path = path
        self._lock = threading.Lock()

    def close(self):
        with self._lock:
            if self.fd is not None:
                # Unlink while still locked. Readers hold admission.lock and never wait
                # on this lock, so no lock inversion or idle gap is possible.
                try:
                    self.path.unlink(missing_ok=True)
                finally:
                    os.close(self.fd)
                    self.fd = None


def reserve(home):
    import fcntl

    # Some callers hold the backend session-map lock. Absorb ordinary short
    # commits, but bound contention to 50ms rather than an updater's timeout.
    with admission_lock(home, timeout=0.05) as root:
        if is_held(root):
            return None
        directory = root / "reservations"
        directory.mkdir(exist_ok=True)
        path = directory / uuid.uuid4().hex
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return Reservation(fd, path)
        except BaseException:
            os.close(fd)
            path.unlink(missing_ok=True)
            raise


def _state_locked(root):
    import fcntl

    count = 0
    directory = root / "reservations"
    try:
        paths = list(directory.iterdir())
    except FileNotFoundError:
        paths = ()
    for path in paths:
        try:
            fd = os.open(path, os.O_RDWR)
        except FileNotFoundError:  # owner completed while we enumerated
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                count += 1
            else:
                path.unlink(missing_ok=True)  # crashed owner; no age/PID heuristic
        finally:
            os.close(fd)
    return {"version": 1, "held": is_held(root), "reservations": count}


def state(home):
    with admission_lock(home) as root:
        return _state_locked(root)


def set_hold(home):
    with admission_lock(home) as root:
        directory = root / "holds"
        directory.mkdir(exist_ok=True)
        fd = os.open(directory / "hermes-sync", os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        # The marker survives updater crashes; removal is always explicit.
        for parent in (directory, root, root.parent):
            fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return _state_locked(root)


def clear_hold(home):
    with admission_lock(home) as root:
        (root / "holds" / "hermes-sync").unlink(missing_ok=True)


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capability", "state", "hold", "release"))
    parser.add_argument("--home", type=Path)
    args = parser.parse_args()
    if args.action == "capability":
        result = CAPABILITY
    else:
        if args.home is None:
            parser.error("--home must name the explicit installation root")
        result = {"state": state, "hold": set_hold, "release": clear_hold}[args.action](args.home)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
