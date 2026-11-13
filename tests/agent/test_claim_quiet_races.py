"""Regression contract for the claim-quiet lease foundation's fork, clock, namespace and cleanup races.

Sibling of ``test_claim_quiet.py``. Every case drives the real ``agent.claim_quiet`` against a temp
root and replaces only a named seam to hold a thread (or fork a copy) at a specific window. Fork
children report through their exit status and always leave with ``os._exit``.

Bounds that these tests do NOT claim: an arbitrary same-user process swapping the lease path right
after the last revalidation cannot be fenced, and a signal-handler fork between a descriptor's
creation and its registration is covered only by refusal on both copies, not by serialization.
"""

from __future__ import annotations

import dataclasses
import errno
import fcntl
import os
import signal
import sys
import threading
from pathlib import Path

import pytest

from agent import claim_quiet as cq
from agent import claim_quiet_records as records
from hermes_platform import quiet_clock
from tests.waiting import wait_until

NS = 1_000_000_000
WAIT = 60.0
forks = pytest.mark.filterwarnings("ignore::DeprecationWarning")  # fork with other threads is the point


class FakeClock:
    def __init__(self, wall=1_800_000_000.0, ns=10**15, boot="boot-a"):
        self.now_wall, self.now_ns, self.boot = wall, ns, boot

    def advance(self, seconds: float) -> None:
        self.now_wall += seconds
        self.now_ns += int(seconds * NS)

    def wall(self):
        return self.now_wall

    def elapsed_ns(self):
        return self.now_ns

    def boot_id(self):
        return self.boot


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    yield root
    assert cq._CUSTODY == set(), "a lease descriptor was left in custody"
    assert cq._OWNERS == {}, "an owner was left registered"


@pytest.fixture
def mp():
    """A patcher independent of ``home``'s, so ``mp.undo()`` never reverts ``HERMES_HOME``."""
    patcher = pytest.MonkeyPatch()
    yield patcher
    patcher.undo()


def _lock_held(path: Path) -> bool:
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _fd_closed(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError as exc:
        return exc.errno == errno.EBADF
    return False


def _fork_and_wait(child) -> int:
    """Fork; the child runs ``child()`` (returns an exit code) under an alarm and never returns."""
    pid = os.fork()
    if pid == 0:
        code = 99
        try:
            signal.alarm(30)  # a hang is a failure with a distinct status, not a stuck suite
            code = child()
        except BaseException:  # the child must report, never unwind into pytest
            import traceback

            traceback.print_exc()
            code = 98
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


class _Forker:
    """A thread that forks (blocking in the fork hook while a custody section is open)."""

    def __init__(self, child):
        self.code: int | None = None
        self._child = child
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        self.code = _fork_and_wait(self._child)

    def start_and_wait_for_hook(self) -> None:
        self.thread.start()
        wait_until(lambda: self.in_prepare_hook() or self.code is not None, timeout=WAIT,
                   what="forking thread reaching the fork hook")

    def in_prepare_hook(self) -> bool:
        frame = sys._current_frames().get(self.thread.ident)
        return frame is not None and frame.f_code.co_name == "_fork_prepare"

    def join(self) -> int:
        self.thread.join(WAIT)
        assert self.code is not None, "fork child never finished"
        return self.code


class _Pause:
    """Park the calling thread at a seam until the test releases it."""

    def __init__(self):
        self.reached, self.release = threading.Event(), threading.Event()

    def park(self) -> None:
        self.reached.set()
        assert self.release.wait(WAIT), "test never released the parked thread"


def _start(target) -> threading.Thread:
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


# ---- F1: fork custody covers open/registration and close/removal -------------------------------


@pytest.mark.platforms("linux")
@forks
def test_other_thread_fork_waits_out_open_registration_and_child_closes_the_descriptors(home, mp):
    pause, inherited, result = _Pause(), [], []
    real_open = cq._open_lock

    def parked_open(dir_fd, **kw):
        fd = real_open(dir_fd, **kw)
        inherited[:] = [dir_fd, fd]
        pause.park()  # inside the custody section, descriptors created but no owner yet
        return fd

    mp.setattr(cq, "_open_lock", parked_open)
    worker = _start(lambda: result.append(cq.acquire(max_s=60, reason="r", clock=FakeClock())))
    assert pause.reached.wait(WAIT)
    assert not cq._FORK_LOCK.acquire(blocking=False), "custody section must exclude other threads' forks"

    ready_r, ready_w = os.pipe()
    hold_r, hold_w = os.pipe()

    def child() -> int:
        os.write(ready_w, b"r")
        os.read(hold_r, 1)
        return 0 if all(_fd_closed(fd) for fd in inherited) else 1

    forker = _Forker(child)
    forker.start_and_wait_for_hook()
    assert forker.code is None and forker.thread.is_alive()  # parked in the hook
    pause.release.set()
    worker.join(WAIT)
    owner = result[0]
    os.read(ready_r, 1)

    # Model abrupt loss of the parent's descriptors (no LOCK_UN): only a leaked child copy could
    # keep the lock now.
    lock_path = cq.quiet_paths().lock
    with cq._custody_section():
        owner._closed = True
        cq._CUSTODY.clear()
        cq._OWNERS.clear()
    os.close(owner._lock_fd)
    os.close(owner._dir_fd)
    assert not _lock_held(lock_path)
    os.write(hold_w, b"x")
    assert forker.join() == 0
    for fd in (ready_r, ready_w, hold_r, hold_w):
        os.close(fd)


@pytest.mark.platforms("linux")
@forks
def test_other_thread_fork_waits_out_close_removal_and_the_lock_is_free_while_the_child_lives(home, mp):
    owner = cq.acquire(max_s=60, reason="r", clock=FakeClock())
    lock_fd, dir_fd = owner._lock_fd, owner._dir_fd
    pause, real_close = _Pause(), os.close
    closer = []

    def parked_close(fd):
        if fd == lock_fd and threading.current_thread() is closer[0]:
            pause.park()  # lock already released and the descriptor already deregistered
        return real_close(fd)

    mp.setattr(cq.os, "close", parked_close)
    worker = threading.Thread(target=owner.close, daemon=True)
    closer.append(worker)
    worker.start()
    assert pause.reached.wait(WAIT)
    assert not cq._FORK_LOCK.acquire(blocking=False)

    ready_r, ready_w = os.pipe()
    hold_r, hold_w = os.pipe()

    def child() -> int:
        # Judged right after the fork, before this process opens anything that could reuse the
        # numbers; the parent reads the verdict before it opens any descriptor of its own.
        ok = _fd_closed(lock_fd) and _fd_closed(dir_fd)
        os.write(ready_w, b"1" if ok else b"0")
        os.read(hold_r, 1)  # stay alive: only a leaked descriptor could keep the lock held
        return 0 if ok else 1

    forker = _Forker(child)
    forker.start_and_wait_for_hook()
    pause.release.set()
    worker.join(WAIT)
    assert os.read(ready_r, 1) == b"1", "the child inherited a lease descriptor that was already released"
    os.close(ready_r)
    os.close(ready_w)
    mp.undo()
    assert not _lock_held(cq.quiet_paths().lock)
    assert cq.inspect(clock=FakeClock()).state == "inactive"
    os.write(hold_w, b"x")
    assert forker.join() == 0, "the child inherited a lease descriptor that was already released"
    os.close(hold_r)
    os.close(hold_w)


@pytest.mark.platforms("linux")
@forks
def test_same_stack_fork_inside_the_custody_section_is_refused_by_both_copies(home, mp):
    real_open, state = cq._open_lock, {}

    def forking_open(*a, **kw):
        fd = real_open(*a, **kw)
        state["pid"] = os.fork()  # a signal-style fork: both copies continue this acquisition
        return fd

    mp.setattr(cq, "_open_lock", forking_open)
    outcome = "acquired"
    try:
        cq.acquire(max_s=60, reason="r", clock=FakeClock())
    except cq.QuietUnavailableError as exc:
        outcome = f"refused: {exc}"
    mp.undo()
    if state["pid"] == 0:  # child copy
        os._exit(0 if outcome.startswith("refused") and not cq._CUSTODY and not cq._OWNERS else 1)
    _, status = os.waitpid(state["pid"], 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert outcome.startswith("refused") and "fork" in outcome
    assert not _lock_held(cq.quiet_paths().lock)
    assert not cq.quiet_paths().lease.exists()
    cq.acquire(max_s=60, reason="after", clock=FakeClock()).close()  # nothing was consumed


@pytest.mark.platforms("linux")
@forks
def test_fork_after_locking_never_lets_the_child_unlock_or_clear_the_parents_lease(home, mp):
    real_take, state = cq._take_lock, {}
    go_r, go_w = os.pipe()

    def forking_take(owner, clock):
        real_take(owner, clock)  # the parent's open file description is now locked
        state["pid"] = os.fork()
        if state["pid"] == 0:
            os.read(go_r, 1)  # let the parent publish first

    mp.setattr(cq, "_take_lock", forking_take)
    clock = FakeClock()
    outcome, owner = "acquired", None
    try:
        owner = cq.acquire(max_s=60, reason="r", clock=clock)
    except cq.QuietUnavailableError:
        outcome = "refused"
    mp.undo()
    if state["pid"] == 0:  # child: must refuse and exit without touching the shared lock
        os._exit(0 if outcome == "refused" else 1)
    assert owner is not None and cq.inspect(clock=clock).state == "active"
    os.write(go_w, b"x")
    _, status = os.waitpid(state["pid"], 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert _lock_held(cq.quiet_paths().lock), "the child unlocked the parent's open file description"
    assert cq.inspect(clock=clock).state == "active" and cq.quiet_paths().lease.exists()
    owner.close()
    assert cq.inspect(clock=clock).state == "inactive"
    os.close(go_r)
    os.close(go_w)


@pytest.mark.platforms("linux")
@forks
def test_signal_handler_fork_inside_the_section_does_not_deadlock_and_is_refused(home, mp):
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("Python signal handlers run on the main thread")
    real_open, state = cq._open_lock, {"pid": None}

    def handler(signum, frame):
        state["pid"] = os.fork()

    def signalling_open(*a, **kw):
        fd = real_open(*a, **kw)
        signal.raise_signal(signal.SIGUSR1)
        return fd

    old = signal.signal(signal.SIGUSR1, handler)
    mp.setattr(cq, "_open_lock", signalling_open)
    outcome = "acquired"
    try:
        cq.acquire(max_s=60, reason="r", clock=FakeClock())
    except cq.QuietUnavailableError:
        outcome = "refused"
    finally:
        signal.signal(signal.SIGUSR1, old)
        mp.undo()
    if state["pid"] == 0:
        os._exit(0 if outcome == "refused" else 1)
    assert state["pid"], "the signal handler never forked"
    _, status = os.waitpid(state["pid"], 0)
    assert os.waitstatus_to_exitcode(status) == 0 and outcome == "refused"
    assert not _lock_held(cq.quiet_paths().lock)


# ---- F2: an inherited locked mutex never deadlocks the child's close ---------------------------


@pytest.mark.platforms("linux")
@forks
def test_child_close_with_an_inherited_held_mutex_returns_and_leaves_the_parent_lease(home):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    with owner._mutex:  # a concurrent parent close would hold exactly this at fork time
        code = _fork_and_wait(lambda: (owner.close(), 0 if owner.closed else 1)[1])
    assert code == 0, f"child close hung or failed (status {code})"
    assert cq.inspect(clock=clock).state == "active" and _lock_held(cq.quiet_paths().lock)
    owner.close()
    assert cq.inspect(clock=clock).state == "inactive"


@pytest.mark.platforms("linux")
@forks
def test_fork_while_another_thread_is_inside_close_leaves_the_child_able_to_close(home):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    pause = _Pause()
    real_clear = owner._clear_own_lease

    def parked_clear():
        pause.park()  # inside close(), owner mutex held, lock not yet released
        real_clear()

    owner._clear_own_lease = parked_clear
    worker = _start(owner.close)
    assert pause.reached.wait(WAIT)
    code = _fork_and_wait(lambda: (owner.close(), 0 if owner.closed else 1)[1])
    assert code == 0, f"child close hung or failed (status {code})"
    assert _lock_held(cq.quiet_paths().lock), "the child's close released the parent's lock"
    pause.release.set()
    worker.join(WAIT)
    assert cq.inspect(clock=clock).state == "inactive"


# ---- F3: every sampled clock value is validated before any logic -------------------------------

BAD_WALL = [float("nan"), float("inf"), float("-inf"), -1.0, True, "1800000000", None]
BAD_ELAPSED = [float("nan"), float("inf"), True, float(10**15), 1.5, -1, "5", None]
BAD_BOOT = [None, "", 7, "x" * 129, "bad\x00boot"]


def _clock_with(**override) -> FakeClock:
    clock = FakeClock()
    for key, value in override.items():
        setattr(clock, key, value)
    return clock


@pytest.mark.parametrize("field,values", [("now_wall", BAD_WALL), ("now_ns", BAD_ELAPSED), ("boot", BAD_BOOT)])
def test_reader_treats_every_invalid_live_clock_sample_as_uncertain(home, field, values):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    try:
        for value in values:
            bad = _clock_with(**{field: value})
            reading = cq.inspect(clock=bad)
            assert reading.state == "uncertain", (field, value, reading)
            assert cq.read_active(clock=bad) is None
        assert cq.inspect(clock=clock).state == "active"
    finally:
        owner.close()


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf"), -5.0, True, "1800000000", 1.5j])
def test_explicit_now_is_validated_even_when_the_real_wall_is_expired(home, now):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    clock.now_wall += 61  # wall deadline passed; elapsed still before its deadline
    assert cq.inspect(clock=clock).state == "inactive"
    assert cq.inspect(clock=clock, now=now).state == "uncertain"
    owner.close()


def test_a_raising_clock_is_uncertain_for_readers_and_refused_for_producers(home):
    class Broken(FakeClock):
        def elapsed_ns(self):
            raise RuntimeError("clock down")

    owner = cq.acquire(max_s=60, reason="r", clock=FakeClock())
    assert cq.inspect(clock=Broken()).state == "uncertain"
    owner.close()
    with pytest.raises(cq.QuietUnavailableError):
        cq.acquire(max_s=60, reason="r", clock=Broken())


@pytest.mark.parametrize("field,values", [("now_wall", BAD_WALL), ("now_ns", BAD_ELAPSED), ("boot", BAD_BOOT)])
def test_producer_refuses_an_invalid_sample_before_any_filesystem_mutation(home, field, values):
    for value in values:
        with pytest.raises(cq.QuietUnavailableError):
            cq.acquire(max_s=60, reason="r", clock=_clock_with(**{field: value}))
        assert not (home / "kanban").exists(), (field, value)


def test_invalid_second_sample_does_not_consume_or_rewrite_the_cooldown(home):
    clock = FakeClock()
    cq.acquire(max_s=60, reason="r", clock=clock).close()
    paths = cq.quiet_paths()
    before = paths.last_started.read_bytes()
    clock.advance(records.COOLDOWN_S + 1)
    for bad in (float("nan"), True):
        with pytest.raises(cq.QuietUnavailableError):
            cq.acquire(max_s=60, reason="r", clock=_clock_with(now_wall=clock.now_wall, now_ns=bad))
    assert paths.last_started.read_bytes() == before
    cq.acquire(max_s=60, reason="r", clock=clock).close()


def test_system_clock_results_get_the_same_validation(home, mp):
    mp.setattr(quiet_clock, "wall_time", lambda: float("nan"))
    with pytest.raises(cq.QuietUnavailableError):
        cq.acquire(max_s=60, reason="r")
    assert cq.inspect().state != "active"
    assert quiet_clock.validate_boot_id(quiet_clock.boot_id())


def test_a_record_the_reader_would_reject_is_never_written_or_returned(home):
    lease = records.QuietLease("a" * 32, 1, 1.0, "i" * 32, "b", 100.0, 160.0, 5, 5 + 60 * NS, 60, "r", "/r", 1, 2)
    assert records.parse_lease(records.dump_checked(lease)) == lease
    for bad in (dataclasses.replace(lease, started_ns=True), dataclasses.replace(lease, started_at=float("nan")),
                dataclasses.replace(lease, deadline_ns=lease.deadline_ns + 1)):
        with pytest.raises(ValueError):
            records.dump_checked(bad)
    with pytest.raises(ValueError):
        records.dump_checked(records.LastStarted("e", 1.0, True, "b"))


# ---- F4: the opened directory is re-bound to its pathname ---------------------------------------


def _swap_after(real, paths, moved_name="detached"):
    moved = paths.directory.with_name(moved_name)

    def swapped(*a, **kw):
        fd = real(*a, **kw)
        os.rename(paths.directory, moved)
        os.mkdir(paths.directory, 0o700)
        return fd

    return swapped, moved


def test_producer_refuses_a_directory_detached_right_after_it_was_opened(home, mp):
    clock = FakeClock()
    paths = cq.quiet_paths()
    paths.directory.parent.mkdir(parents=True)
    swapped, moved = _swap_after(cq._open_dir, paths)
    with mp.context() as patch:
        patch.setattr(cq, "_open_dir", swapped)
        with pytest.raises(cq.QuietUnavailableError, match="detached"):
            cq.acquire(max_s=60, reason="first", clock=clock)
    assert not (moved / records.LEASE_NAME).exists() and not (moved / records.LAST_STARTED_NAME).exists()
    assert not _lock_held(moved / records.LOCK_NAME)
    second = cq.acquire(max_s=60, reason="second", clock=clock)  # nothing was published or consumed
    assert cq.read_active(clock=clock) == second.lease
    second.close()


def test_producer_detects_the_directory_detached_during_publication(home, mp):
    clock = FakeClock()
    paths = cq.quiet_paths()
    real_write, moved = records.write_atomic, paths.directory.with_name("detached")

    def write_then_detach(dir_fd, name, payload):
        real_write(dir_fd, name, payload)
        if name == records.LEASE_NAME:
            os.rename(paths.directory, moved)
            os.mkdir(paths.directory, 0o700)

    mp.setattr(records, "write_atomic", write_then_detach)
    with pytest.raises(cq.QuietUnavailableError, match="detached"):
        cq.acquire(max_s=60, reason="r", clock=clock)
    mp.undo()
    assert not _lock_held(moved / records.LOCK_NAME), "the refused producer must release its lock"
    assert not (moved / records.LEASE_NAME).exists(), "its own lease is cleared through its descriptor"
    assert cq.inspect(clock=clock).state != "active"


def test_reader_is_uncertain_when_the_directory_is_detached_after_it_opens_it(home, mp):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    paths = cq.quiet_paths()
    swapped, _moved = _swap_after(cq._open_dir, paths)
    with mp.context() as patch:
        patch.setattr(cq, "_open_dir", swapped)
        raced = cq.inspect(clock=clock)
    assert raced.state == "uncertain" and "detached" in raced.reason
    assert cq.inspect(clock=clock).state == "inactive"  # the canonical path really has no lease
    owner.close()


def test_reader_is_uncertain_when_the_directory_is_detached_before_final_verification(home, mp):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    paths = cq.quiet_paths()
    real_check, moved = cq._check_producer, paths.directory.with_name("detached")

    def check_then_detach(lease):
        real_check(lease)
        os.rename(paths.directory, moved)
        os.mkdir(paths.directory, 0o700)

    mp.setattr(cq, "_check_producer", check_then_detach)
    raced = cq.inspect(clock=clock)
    mp.undo()
    assert raced.state == "uncertain" and cq.read_active(clock=clock) is None
    owner.close()


def test_a_replaced_parent_directory_is_detected_by_both_sides(home):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    paths = cq.quiet_paths()
    kanban = paths.directory.parent
    kanban.rename(home / "kanban-old")
    paths.directory.parent.mkdir()
    paths.directory.mkdir(mode=0o700)
    assert cq.inspect(clock=clock).state != "active"
    owner.close()


# ---- F5: independent descriptor cleanup, original failures preserved ---------------------------


def test_lock_descriptor_close_failure_still_closes_the_directory_and_is_not_retried(home, mp):
    owner = cq.acquire(max_s=60, reason="r", clock=FakeClock())
    lock_fd, dir_fd = owner._lock_fd, owner._dir_fd
    real_close, attempts = os.close, []

    def failing_close(fd):
        attempts.append(fd)
        if fd == lock_fd:
            raise OSError(errno.EIO, "injected close failure")
        return real_close(fd)

    mp.setattr(cq.os, "close", failing_close)
    with pytest.raises(OSError, match="injected close failure"):
        owner.close()
    owner.close()  # a later close is a no-op: the ambiguous descriptor is never re-closed
    mp.undo()
    assert attempts.count(lock_fd) == 1 and dir_fd in attempts
    assert _fd_closed(dir_fd) and owner.closed
    assert not _lock_held(cq.quiet_paths().lock)  # LOCK_UN ran before the failing close
    assert lock_fd not in cq._CUSTODY
    real_close(lock_fd)  # the injected failure left it open; release it for the test


def test_lock_release_failure_still_closes_both_descriptors(home, mp):
    owner = cq.acquire(max_s=60, reason="r", clock=FakeClock())
    lock_fd, dir_fd = owner._lock_fd, owner._dir_fd
    real_flock = fcntl.flock

    def failing_flock(fd, op):
        if fd == lock_fd and op == fcntl.LOCK_UN:
            raise OSError(errno.EIO, "injected unlock failure")
        return real_flock(fd, op)

    mp.setattr(cq.fcntl, "flock", failing_flock)
    with pytest.raises(OSError, match="injected unlock failure"):
        owner.close()
    mp.undo()
    assert _fd_closed(lock_fd) and _fd_closed(dir_fd)
    assert not _lock_held(cq.quiet_paths().lock)  # closing the last descriptor released the lock


def test_start_failure_stays_primary_when_cleanup_also_fails(home, mp):
    real_write, real_close = records.write_atomic, os.close
    holder = {}

    def failing_write(dir_fd, name, payload):
        if name == records.LEASE_NAME:
            raise OSError(errno.ENOSPC, "injected publish failure")
        return real_write(dir_fd, name, payload)

    def failing_close(fd):
        if fd == holder.get("lock"):
            raise OSError(errno.EIO, "injected close failure")
        return real_close(fd)

    real_open_lock = cq._open_lock

    def remembering_open(*a, **kw):
        holder["lock"] = real_open_lock(*a, **kw)
        return holder["lock"]

    mp.setattr(records, "write_atomic", failing_write)
    mp.setattr(cq, "_open_lock", remembering_open)
    mp.setattr(cq.os, "close", failing_close)
    with pytest.raises(cq.QuietUnavailableError) as caught:
        cq.acquire(max_s=60, reason="r", clock=FakeClock())
    mp.undo()
    original = caught.value.__cause__
    assert isinstance(original, OSError) and original.errno == errno.ENOSPC
    assert any("injected close failure" in note for note in original.__notes__)
    assert cq.quiet_paths().last_started.exists()  # the cooldown stays consumed
    assert not _lock_held(cq.quiet_paths().lock)
    real_close(holder["lock"])


# ---- custody bookkeeping ------------------------------------------------------------------------


def test_custody_is_empty_after_success_and_after_every_refusal(home):
    clock = FakeClock()
    cq.acquire(max_s=60, reason="r", clock=clock).close()
    assert cq._CUSTODY == set()
    with pytest.raises(cq.QuietCooldownError):
        cq.acquire(max_s=60, reason="r", clock=clock)
    assert cq._CUSTODY == set() and cq._OWNERS == {}
    clock.advance(records.COOLDOWN_S + 1)
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    assert cq._CUSTODY == {owner._lock_fd, owner._dir_fd}
    owner.close()
