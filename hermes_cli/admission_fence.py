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


# [daveey] The longest designed admission.lock hold: a hub Kanban claim's independent ~1 s CPU sample (window <= 2 s,
# else refused) plus its commit, taken entirely under this lock (worker-first overflow, docs/worker-first-v1.md).
# Turn sources wait longer than this (backend_retirement.BUSY_WAIT_S), so a hold never refuses a chat turn.
CLAIM_MAX_HOLD_S = 2.5
# A turn source waiting out a busy lock registers here; a Kanban claim that sees a waiter defers to its next tick
# (claims retry; a typed message, notification or review turn has nowhere else to go), so waits stay one hold long.
TURN_WAITERS = "turn-waiters"


class AdmissionBusy(TimeoutError):
    """admission.lock is held by another process past the caller's timeout; no hold is implied.

    [daveey] Ordinary holders (hermes-sync's state read, its hold write with fsyncs, a Kanban claim
    through its commit) can keep the lock past 50 ms on a loaded Mac. Callers that may wait retry on
    this type (backend_retirement); it subclasses TimeoutError, so an ``except TimeoutError`` still
    fails closed."""


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
                    raise AdmissionBusy("Hermes update admission lock is busy") from None
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


@contextmanager
def turn_waiting(home):
    """Mark a turn source as waiting for admission.lock while the block runs (never takes the lock).

    The marker is flocked before it becomes visible (dotfile, then rename), so a reader never mistakes a
    half-created marker for a crashed one; a crashed owner's marker is unlocked and is reaped by the reader."""
    import fcntl

    directory = Path(home) / "core-update" / TURN_WAITERS
    directory.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex
    tmp, path = directory / f".{name}", directory / name
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.rename(tmp, path)
        yield
    finally:
        try:
            path.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)
        finally:
            os.close(fd)


def turns_waiting(root):
    """True while a live turn source waits for admission.lock (``root`` = the ``core-update`` directory)."""
    import fcntl

    try:
        paths = [p for p in (Path(root) / TURN_WAITERS).iterdir() if not p.name.startswith(".")]
    except FileNotFoundError:
        return False
    for path in paths:
        try:
            fd = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                return True
            path.unlink(missing_ok=True)  # crashed owner
        finally:
            os.close(fd)
    return False


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


def extend(home):
    """A reservation taken WITHOUT the hold check, for work already admitted under a live reservation
    the caller still holds (a cron run registered inside its tick's reservation). The updater reads
    reservations after setting the hold, so it sees the caller's or this one: never neither. [daveey]"""
    import fcntl

    with admission_lock(home, timeout=0.05) as root:
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


# [daveey] Core-update pause request (t_01a1827558218fed). hermes-sync's drain window writes this JSON
# file beside the hold ({"version": 1, "expires_at": <epoch s>, ...}) to ask turns the next backend can
# auto-continue to end at their next step boundary. It counts only while the hold stands and before
# expires_at, so a file a crashed drainer leaves behind never outlives its window. hermes-sync writes it
# directly (no core call), so the format is the contract: keep it backward compatible.
PAUSE_REQUEST = "pause-request"


def pause_requested(home):
    """Cheap boundary check: one stat when no request exists. Never raises."""
    root = Path(home) / "core-update"
    try:
        (root / PAUSE_REQUEST).stat()
    except OSError:
        return False
    try:
        body = json.loads((root / PAUSE_REQUEST).read_text(encoding="utf-8-sig"))
        expires_at = float(body["expires_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return False
    try:
        return expires_at > time.time() and is_held(root)
    except OSError:
        return False


def request_pause(home, *, expires_at, pid=None):
    root = Path(home) / "core-update"
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".{PAUSE_REQUEST}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps({"version": 1, "pid": pid or os.getpid(), "requested_at": time.time(),
                               "expires_at": float(expires_at)}) + "\n", encoding="utf-8")
    os.replace(tmp, root / PAUSE_REQUEST)


def clear_pause_request(home):
    (Path(home) / "core-update" / PAUSE_REQUEST).unlink(missing_ok=True)


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
