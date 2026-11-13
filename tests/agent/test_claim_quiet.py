"""Behaviour contract of the root-scoped claim-quiet lease foundation (``agent.claim_quiet``).

Real imported lease module against a temp root; cross-process cases run real subprocesses that
import the module. Admission must fail OPEN on any uncertainty while observation fails CLOSED
(``inspect().state != "active"``). Native Linux clock/lock behaviour is exercised here; the Darwin
clock is justified from source in ``hermes_platform/quiet_clock.py`` and is not executed by this file.
"""

from __future__ import annotations

import dataclasses
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from agent import claim_quiet as cq
from agent import claim_quiet_records as records
from hermes_platform import quiet_clock
from tests.waiting import wait_until

REPO = Path(__file__).resolve().parents[2]
NS = 1_000_000_000


class FakeClock:
    """Injectable wall + suspend-inclusive clock so 2h cooldowns need no real waiting."""

    def __init__(self, wall: float = 1_800_000_000.0, ns: int = 10**15, boot: str | None = "boot-a"):
        self.now_wall, self.now_ns, self.boot = wall, ns, boot

    def advance(self, seconds: float) -> None:
        self.now_wall += seconds
        self.now_ns += int(seconds * NS)

    def wall(self) -> float:
        return self.now_wall

    def elapsed_ns(self):
        return self.now_ns

    def boot_id(self):
        return self.boot


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "profiles" / "alt").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def _fd_dir() -> int:
    return os.open(cq.quiet_paths().directory, os.O_RDONLY | os.O_DIRECTORY)


def _rewrite_lease(**changes) -> None:
    paths = cq.quiet_paths()
    lease = records.parse_lease(paths.lease.read_bytes())
    _write_lease_bytes(records.dump(dataclasses.replace(lease, **changes)))


def _write_lease_bytes(payload: bytes) -> None:
    fd = _fd_dir()
    try:
        records.write_atomic(fd, records.LEASE_NAME, payload)
    finally:
        os.close(fd)


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in directory.iterdir()}


# ---- paths / validation -----------------------------------------------------------------------


def test_paths_are_the_call_time_root_not_the_active_profile(home, monkeypatch):
    paths = cq.quiet_paths()
    assert paths.directory == home / "kanban" / "claim-quiet"
    assert paths.lock.name == "producer.lock" and paths.lease.name == "lease.json"
    assert paths.last_started.name == "last-started.json"
    monkeypatch.setenv("HERMES_HOME", str(home / "profiles" / "alt"))
    assert cq.quiet_paths().directory == home / "kanban" / "claim-quiet"

    from agent import file_safety

    elsewhere = home.parent / "other-root"
    monkeypatch.setattr(file_safety, "_hermes_root_path", lambda: elsewhere)
    assert cq.quiet_paths().directory == elsewhere / "kanban" / "claim-quiet"


@pytest.mark.parametrize("max_s", [0, -1, 481, True, 1.5, "60", None])
def test_acquire_rejects_bad_duration_before_touching_the_filesystem(home, max_s):
    with pytest.raises(ValueError):
        cq.acquire(max_s=max_s, reason="r")
    assert not (home / "kanban").exists()


@pytest.mark.parametrize("reason", ["", "   ", "x" * 201, "bad\x00reason", 7, None])
def test_acquire_rejects_bad_reason(home, reason):
    with pytest.raises(ValueError):
        cq.acquire(max_s=60, reason=reason)
    assert not (home / "kanban").exists()


# ---- lease lifecycle --------------------------------------------------------------------------


def test_acquire_publishes_a_verified_lease_and_close_clears_it(home):
    clock = FakeClock()
    owner = cq.acquire(max_s=480, reason="measure", clock=clock)
    lease = owner.lease
    assert cq.read_active(clock=clock) == lease
    assert cq.inspect(clock=clock).state == "active"
    assert (lease.max_s, lease.reason, lease.producer_pid) == (480, "measure", os.getpid())
    assert lease.deadline_ns - lease.started_ns == 480 * NS
    assert lease.expires_at - lease.started_at == 480
    paths = cq.quiet_paths()
    st = paths.lock.stat()
    assert (lease.lock_dev, lease.lock_ino) == (st.st_dev, st.st_ino)
    assert oct(paths.directory.stat().st_mode & 0o777) == "0o700"
    assert oct(st.st_mode & 0o777) == "0o600"

    owner.close()
    owner.close()  # idempotent
    assert owner.closed and not paths.lease.exists()
    reading = cq.inspect(clock=clock)
    assert (reading.state, cq.read_active(clock=clock)) == ("inactive", None)
    assert paths.lock.stat().st_ino == st.st_ino  # the lock inode is permanent


def test_owner_is_a_context_manager_releasing_on_exception(home):
    clock = FakeClock()
    with pytest.raises(RuntimeError):
        with cq.acquire(max_s=60, reason="r", clock=clock):
            assert cq.read_active(clock=clock)
            raise RuntimeError("observer failed")
    assert cq.read_active(clock=clock) is None


def test_reader_never_creates_or_repairs_anything(home):
    clock = FakeClock()
    assert cq.inspect(clock=clock).state == "inactive"
    assert not (home / "kanban").exists()
    cq.acquire(max_s=60, reason="r", clock=clock).close()
    paths = cq.quiet_paths()
    paths.lease.write_bytes(b"{garbage")
    before = _snapshot(paths.directory)
    assert cq.inspect(clock=clock).state == "inactive"  # unlocked: certainly no suppression
    assert _snapshot(paths.directory) == before


def test_rapid_release_reacquire_keeps_one_lock_inode(home):
    clock = FakeClock()
    inodes, episodes = set(), set()
    for _ in range(5):
        owner = cq.acquire(max_s=30, reason="again", clock=clock)
        episodes.add(owner.lease.episode_id)
        inodes.add((owner.lease.lock_dev, owner.lease.lock_ino))
        assert cq.read_active(clock=clock) == owner.lease
        owner.close()
        assert cq.read_active(clock=clock) is None
        clock.advance(records.COOLDOWN_S)
    assert len(episodes) == 5 and len(inodes) == 1


def test_stale_owner_close_never_clears_a_successor_lease(home):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="old", clock=clock)
    successor = dataclasses.replace(owner.lease, episode_id="f" * 32)
    _write_lease_bytes(records.dump(successor))
    owner.close()
    assert records.parse_lease(cq.quiet_paths().lease.read_bytes()) == successor


# ---- expiry: wall AND suspend-inclusive deadline ----------------------------------------------


def test_admission_opens_at_the_wall_deadline(home):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    clock.now_wall += 59
    assert cq.read_active(clock=clock)
    clock.now_wall += 1  # wall alone reaches expiry; the elapsed clock has not moved
    reading = cq.inspect(clock=clock)
    assert (reading.state, reading.reason) == ("inactive", "lease expired")
    owner.close()


def test_admission_opens_when_suspend_inclusive_clock_passes_deadline_despite_frozen_wall(home):
    """Sleep: wall (and a monotonic that pauses in sleep) look unchanged, the boot clock moved on."""
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    clock.now_ns += 61 * NS
    assert cq.read_active(clock=clock) is None
    assert cq.inspect(clock=clock).reason == "lease expired"
    owner.close()


def test_wall_rollback_cannot_extend_the_elapsed_deadline(home):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    clock.now_ns += 61 * NS
    clock.now_wall -= 3600
    assert cq.read_active(clock=clock) is None
    owner.close()


def test_explicit_now_is_the_wall_clock_used(home):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    assert cq.read_active(now=clock.now_wall + 10, clock=clock)
    assert cq.read_active(now=clock.now_wall + 60, clock=clock) is None
    owner.close()


def test_unknown_clock_or_boot_never_extends_a_lease(home):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    clock.boot = None
    assert cq.read_active(clock=clock) is None and cq.inspect(clock=clock).state == "uncertain"
    clock.boot = "boot-a"
    clock.now_ns = None
    assert cq.read_active(clock=clock) is None and cq.inspect(clock=clock).state == "uncertain"
    owner.close()
    with pytest.raises(cq.QuietUnavailableError):
        cq.acquire(max_s=60, reason="r", clock=FakeClock(boot=None))
    with pytest.raises(cq.QuietUnavailableError):
        cq.acquire(max_s=60, reason="r", clock=SimpleNamespace(
            wall=lambda: 1.0, elapsed_ns=lambda: None, boot_id=lambda: "b"))


def test_host_without_a_suspend_inclusive_clock_refuses_lease_and_reads_open(home, monkeypatch):
    monkeypatch.setattr(quiet_clock, "_clock_id", lambda: None)
    assert quiet_clock.suspend_inclusive_ns() is None and not quiet_clock.lease_supported()
    with pytest.raises(cq.QuietUnavailableError):
        cq.acquire(max_s=60, reason="r")
    assert cq.read_active() is None and cq.inspect().state == "uncertain"


# ---- cooldown ---------------------------------------------------------------------------------


def test_two_hour_cooldown_runs_from_start_not_release_without_real_waiting(home):
    clock = FakeClock()
    cq.acquire(max_s=30, reason="first", clock=clock).close()  # released after 0s
    clock.advance(records.COOLDOWN_S - 1)
    with pytest.raises(cq.QuietCooldownError) as err:
        cq.acquire(max_s=30, reason="second", clock=clock)
    assert 0 < err.value.remaining_s <= 1
    clock.advance(1)
    cq.acquire(max_s=30, reason="second", clock=clock).close()


def test_wall_jump_forward_cannot_shorten_a_same_boot_cooldown(home):
    clock = FakeClock()
    cq.acquire(max_s=30, reason="first", clock=clock).close()
    clock.now_wall += 10 * records.COOLDOWN_S  # wall stepped; the boot clock barely moved
    clock.now_ns += 5 * NS
    with pytest.raises(cq.QuietCooldownError):
        cq.acquire(max_s=30, reason="second", clock=clock)


def test_reboot_uses_wall_age_only(home):
    clock = FakeClock()
    cq.acquire(max_s=30, reason="first", clock=clock).close()
    clock.boot, clock.now_ns = "boot-b", 5 * NS  # a new boot has a smaller, unrelated elapsed clock
    clock.now_wall += records.COOLDOWN_S - 10
    with pytest.raises(cq.QuietCooldownError):
        cq.acquire(max_s=30, reason="second", clock=clock)
    clock.advance(10)
    cq.acquire(max_s=30, reason="second", clock=clock).close()


@pytest.mark.parametrize("backwards", ["wall", "elapsed"])
def test_backwards_clocks_refuse_rather_than_shorten_cooldown(home, backwards):
    clock = FakeClock()
    cq.acquire(max_s=30, reason="first", clock=clock).close()
    if backwards == "wall":
        # Same boot: the elapsed clock is authoritative, so a wall step back cannot shorten it either.
        clock.now_wall -= 100
        with pytest.raises(cq.QuietCooldownError):
            cq.acquire(max_s=30, reason="second", clock=clock)
        return
    clock.now_ns -= 100 * NS
    with pytest.raises(cq.QuietUnavailableError, match="incoherent"):
        cq.acquire(max_s=30, reason="second", clock=clock)


def test_corrected_future_wall_time_does_not_wedge_cooldown_on_same_boot(home):
    """A record written while the wall clock was ahead (later corrected) must not refuse every start
    until real time catches up: on the same boot the elapsed clock alone decides."""
    clock = FakeClock()
    clock.now_wall += 10 * 24 * 3600  # wall clock 10 days ahead at the first start
    cq.acquire(max_s=30, reason="first", clock=clock).close()
    clock.now_wall -= 10 * 24 * 3600  # NTP corrects it
    clock.now_ns += (records.COOLDOWN_S + 1) * NS
    cq.acquire(max_s=30, reason="second", clock=clock).close()


@pytest.mark.parametrize("payload", [b"{", b"[]", b'{"schema": 1}', b"\xff\xfe", b""])
def test_malformed_cooldown_refuses_it_is_never_erased_to_bypass_two_hours(home, payload):
    clock = FakeClock()
    cq.acquire(max_s=30, reason="first", clock=clock).close()
    paths = cq.quiet_paths()
    paths.last_started.write_bytes(payload)
    with pytest.raises(cq.QuietUnavailableError, match="cooldown record"):
        cq.acquire(max_s=30, reason="second", clock=clock)
    assert paths.last_started.read_bytes() == payload
    assert cq.read_active(clock=clock) is None


def test_cooldown_is_committed_before_the_lease_and_publication_failure_consumes_it(home, monkeypatch):
    clock = FakeClock()
    real, order = records.write_atomic, []

    def spy(dir_fd, name, payload):
        if name == records.LEASE_NAME:
            committed = records.parse_last_started((cq.quiet_paths().last_started).read_bytes())
            order.append(("lease", committed.episode_id))
            raise OSError(28, "No space left on device")
        order.append((name, None))
        real(dir_fd, name, payload)

    with monkeypatch.context() as patched:
        patched.setattr(records, "write_atomic", spy)
        with pytest.raises(cq.QuietUnavailableError):
            cq.acquire(max_s=60, reason="r", clock=clock)
    assert [n for n, _ in order] == [records.LAST_STARTED_NAME, "lease"]
    paths = cq.quiet_paths()
    consumed = records.parse_last_started(paths.last_started.read_bytes())
    assert consumed.episode_id == order[1][1]
    assert not paths.lease.exists() and cq.read_active(clock=clock) is None
    # the failed start released the lock (not busy) but its cooldown stays consumed
    with pytest.raises(cq.QuietCooldownError):
        cq.acquire(max_s=60, reason="r", clock=clock)
    assert not [p for p in paths.directory.iterdir() if p.name.endswith(".tmp")]


def test_first_start_failure_before_cooldown_commit_does_not_consume_it(home):
    clock = FakeClock()
    bad = SimpleNamespace(wall=clock.wall, elapsed_ns=lambda: None, boot_id=clock.boot_id)
    with pytest.raises(cq.QuietUnavailableError):
        cq.acquire(max_s=60, reason="r", clock=bad)
    assert not cq.quiet_paths().last_started.exists()
    cq.acquire(max_s=60, reason="r", clock=clock).close()


# ---- strict record validation: fail open for admission, closed for observation ----------------


def _valid_lease_json(**over) -> dict:
    lease = dataclasses.asdict(cq.QuietLease(
        "a" * 32, os.getpid(), 1.0, "i" * 32, "boot-a", 100.0, 160.0, 5, 5 + 60 * NS, 60, "r", "/r", 1, 2))
    lease.update(schema=1)
    lease.update(over)
    return lease


def _dump(obj) -> bytes:
    import json

    return json.dumps(obj).encode()


def test_baseline_record_parses():
    assert records.parse_lease(_dump(_valid_lease_json())).max_s == 60


@pytest.mark.parametrize("over", [
    {"max_s": True}, {"max_s": 60.0}, {"max_s": "60"}, {"max_s": 481, "deadline_ns": 5 + 481 * NS},
    {"max_s": 0, "deadline_ns": 5}, {"producer_pid": True}, {"producer_pid": 0},
    {"started_at": True}, {"started_at": "100"}, {"started_at": -1}, {"expires_at": 161.0},
    {"deadline_ns": 5 + 61 * NS}, {"deadline_ns": float(5 + 60 * NS)}, {"started_ns": -1},
    {"episode_id": ""}, {"episode_id": 7}, {"reason": "x" * 201}, {"reason": "a\nb"},
    {"schema": 2}, {"schema": True}, {"lock_ino": None},
])
def test_lease_record_rejects_wrong_types_and_incoherent_values(over):
    with pytest.raises(records.RecordError):
        records.parse_lease(_dump(_valid_lease_json(**over)))


def test_lease_record_rejects_shape_nonfinite_and_junk():
    good = _valid_lease_json()
    for raw in (b"", b"not json", b"[]", b"\xff", _dump({**good, "extra": 1}),
                _dump({k: v for k, v in good.items() if k != "reason"}),
                _dump(good).replace(b'"started_at": 100.0', b'"started_at": NaN')):
        with pytest.raises(records.RecordError):
            records.parse_lease(raw)


@pytest.mark.parametrize("change", [
    {"boot_id": "boot-z"}, {"root": "/elsewhere"}, {"lock_ino": 1}, {"lock_dev": 1},
    {"started_at": 4_000_000_000.0, "expires_at": 4_000_000_060.0},
])
def test_identity_mismatches_open_admission_and_invalidate_observation(home, change):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    assert cq.read_active(clock=clock)
    _rewrite_lease(**change)
    assert cq.read_active(clock=clock) is None
    assert cq.inspect(clock=clock).state == "uncertain"
    owner.close()


@pytest.mark.parametrize("payload", [b"{garbage", b"", b"[]", b"x" * (records.MAX_RECORD_BYTES + 1)])
def test_malformed_lease_with_held_lock_is_uncertain_not_active(home, payload):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    _write_lease_bytes(payload)
    assert cq.read_active(clock=clock) is None
    reading = cq.inspect(clock=clock)
    assert reading.state == "uncertain" and "lease record" in reading.reason
    owner.close()


def test_missing_lease_with_held_lock_is_not_active(home):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    cq.quiet_paths().lease.unlink()
    assert cq.read_active(clock=clock) is None
    assert cq.inspect(clock=clock).state == "inactive"
    owner.close()


def test_pid_reuse_at_the_identity_seam_opens_admission(home, monkeypatch):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    real = cq._process_start
    with monkeypatch.context() as patched:
        patched.setattr(cq, "_process_start", lambda pid: real(pid) + 1.0)
        assert cq.read_active(clock=clock) is None
        assert cq.inspect(clock=clock).reason == "producer PID now belongs to another process"
    assert cq.read_active(clock=clock)
    owner.close()


def test_unreadable_producer_identity_is_uncertain_not_dead(home, monkeypatch):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)

    def denied(pid):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(cq, "_process_start", denied)
    assert cq.read_active(clock=clock) is None
    assert cq.inspect(clock=clock).state == "uncertain"
    owner.close()


def test_unexpected_reader_fault_fails_open_and_stays_uncertain(home, monkeypatch):
    def boom(clock, now):
        raise RuntimeError("bug")

    monkeypatch.setattr(cq, "_inspect", boom)
    assert cq.read_active() is None
    reading = cq.inspect()
    assert (reading.state, reading.reason) == ("uncertain", "unexpected RuntimeError")


def test_non_private_directory_is_refused_by_producer_and_reader(home):
    clock = FakeClock()
    cq.acquire(max_s=30, reason="r", clock=clock).close()
    cq.quiet_paths().directory.chmod(0o770)
    with pytest.raises(cq.QuietUnavailableError):
        cq.acquire(max_s=30, reason="r", clock=FakeClock(wall=clock.now_wall + 10**6))
    assert cq.inspect(clock=clock).state == "uncertain"


# ---- lock inode identity ----------------------------------------------------------------------


def test_replaced_lock_inode_is_detected_and_never_trusted(home):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    paths = cq.quiet_paths()
    old = paths.lock.stat().st_ino
    paths.lock.unlink()
    paths.lock.write_bytes(b"")  # a new, unlocked inode at the same path
    assert paths.lock.stat().st_ino != old
    assert cq.read_active(clock=clock) is None  # the new inode is unlocked
    assert cq.inspect(clock=clock).state == "inactive"
    owner.close()
    assert cq.read_active(clock=clock) is None


def test_symlinked_lock_is_not_followed(home):
    clock = FakeClock()
    cq.acquire(max_s=30, reason="r", clock=clock).close()
    paths = cq.quiet_paths()
    target = home / "elsewhere.lock"
    target.write_bytes(b"")
    paths.lock.unlink()
    paths.lock.symlink_to(target)
    assert cq.inspect(clock=clock).state == "uncertain"
    with pytest.raises(cq.QuietUnavailableError):
        cq.acquire(max_s=30, reason="r", clock=FakeClock(wall=clock.now_wall + 10**6))


def test_a_lock_replaced_between_open_and_lock_is_rejected(home):
    cq.acquire(max_s=30, reason="r", clock=FakeClock()).close()
    paths = cq.quiet_paths()
    dir_fd = _fd_dir()
    try:
        fd = cq._open_lock(dir_fd, create=False)
        paths.lock.unlink()
        paths.lock.write_bytes(b"")
        with pytest.raises(OSError, match="replaced"):
            cq._require_stable_lock(dir_fd, fd)
        os.close(fd)
    finally:
        os.close(dir_fd)


# ---- descriptor inheritance -------------------------------------------------------------------


def test_exec_cannot_inherit_the_lock_descriptor(home):
    owner = cq.acquire(max_s=60, reason="r", clock=FakeClock())
    fd = owner._lock_fd
    assert not os.get_inheritable(fd) and not os.get_inheritable(owner._dir_fd)
    probe = ("import os, sys\nfd = int(sys.argv[1])\n"
             "try:\n    os.fstat(fd)\nexcept OSError:\n    print('closed')\nelse:\n    print('INHERITED')\n")
    out = subprocess.run([sys.executable, "-c", probe, str(fd)], close_fds=False, capture_output=True,
                         text=True, timeout=60, check=True).stdout
    owner.close()
    assert out.strip() == "closed"


# ---- shared probes vs bounded producer retry --------------------------------------------------


def _hold_shared_probes(count: int) -> list[int]:
    import fcntl

    fds = []
    for _ in range(count):
        fd = os.open(cq.quiet_paths().lock, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fds.append(fd)
    return fds


def _fake_time(on_sleep):
    state = {"now": 0.0, "slept": []}

    def sleep(delay):
        state["slept"].append(delay)
        state["now"] += delay
        on_sleep(len(state["slept"]))

    return state, SimpleNamespace(monotonic=lambda: state["now"], sleep=sleep)


def test_concurrent_shared_reader_probes_do_not_fail_the_producer(home, monkeypatch):
    clock = FakeClock()
    cq.acquire(max_s=30, reason="seed", clock=clock).close()
    clock.advance(records.COOLDOWN_S)
    probes = _hold_shared_probes(3)

    def release_probes(call):
        while probes:
            os.close(probes.pop())

    state, fake = _fake_time(release_probes)
    monkeypatch.setattr(cq, "time", fake)
    owner = cq.acquire(max_s=30, reason="after probes", clock=clock)
    assert state["slept"], "the producer must have retried instead of failing"
    assert cq.read_active(clock=clock) == owner.lease
    owner.close()


def test_persistent_probe_contention_ends_busy_within_one_second_of_retry(home, monkeypatch):
    clock = FakeClock()
    cq.acquire(max_s=30, reason="seed", clock=clock).close()
    clock.advance(records.COOLDOWN_S)
    probes = _hold_shared_probes(1)
    state, fake = _fake_time(lambda call: None)
    monkeypatch.setattr(cq, "time", fake)
    try:
        with pytest.raises(cq.QuietBusyError, match="contended"):
            cq.acquire(max_s=30, reason="r", clock=clock)
    finally:
        for fd in probes:
            os.close(fd)
    assert records.PRODUCER_RETRY_S <= sum(state["slept"]) <= records.PRODUCER_RETRY_S + 0.06
    assert cq.read_active(clock=clock) is None


def test_competing_active_producer_is_refused_at_once_without_retrying(home, monkeypatch):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="holder", clock=clock)
    state, fake = _fake_time(lambda call: None)
    monkeypatch.setattr(cq, "time", fake)
    with pytest.raises(cq.QuietBusyError, match="active"):
        cq.acquire(max_s=60, reason="second", clock=clock)
    assert state["slept"] == []
    owner.close()


def test_a_reader_is_never_blocked_by_a_held_producer_lock(home):
    clock = FakeClock()
    owner = cq.acquire(max_s=60, reason="r", clock=clock)
    results: queue.Queue = queue.Queue()
    threads = [threading.Thread(target=lambda: results.put(cq.read_active(clock=clock))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert [results.get_nowait() for _ in threads] == [owner.lease] * 8
    owner.close()


# ---- real subprocess behaviour ----------------------------------------------------------------

_HOLDER = r"""
import sys
from agent import claim_quiet as cq
print("READY", flush=True)
sys.stdin.readline()
try:
    owner = cq.acquire(max_s=int(sys.argv[1]), reason="child")
except cq.QuietCooldownError:
    print("COOLDOWN", flush=True); owner = None
except cq.QuietBusyError:
    print("BUSY", flush=True); owner = None
else:
    print("OK", owner.lease.episode_id, flush=True)
sys.stdin.readline()
if owner:
    owner.close()
print("CLOSED", flush=True)
"""

_FORK = r"""
import os
from agent import claim_quiet as cq
owner = cq.acquire(max_s=60, reason="fork")
fd = owner._lock_fd
to_parent, from_child = os.pipe()
to_child, from_parent = os.pipe()
pid = os.fork()
if pid == 0:
    try:
        os.fstat(fd); fd_open = 1
    except OSError:
        fd_open = 0
    owner.close()  # a child must neither unlock nor clear the parent's lease
    os.write(from_child, f"{fd_open} {int(owner.closed)} {len(cq._OWNERS)}\n".encode())
    os.read(to_child, 1)
    os._exit(0)
print("CHILD", os.read(to_parent, 100).decode().strip(), flush=True)
print("AFTER_CHILD_CLOSE", cq.inspect().state, flush=True)
owner.close()
print("AFTER_PARENT_CLOSE", cq.inspect().state, flush=True)  # the child is still alive
os.write(from_parent, b"x")
os.waitpid(pid, 0)
"""

_FAULTY = r"""
import os, subprocess, sys
from agent import claim_quiet as cq
owner = cq.acquire(max_s=60, reason="faulty")
holder = subprocess.Popen(
    [sys.executable, "-c", "import sys; print('HOLD', flush=True); sys.stdin.read()"],
    pass_fds=[owner._lock_fd], stdin=int(sys.argv[1]), stdout=subprocess.PIPE, text=True)
holder.stdout.readline()
print("PIDS", os.getpid(), holder.pid, flush=True)
sys.stdin.read()
"""


class Child:
    def __init__(self, code: str, home: Path, *args: str, pass_fds: tuple[int, ...] = ()):
        env = {**os.environ, "HERMES_HOME": str(home)}
        env.pop("PYTHONPATH", None)
        self.proc = subprocess.Popen([sys.executable, "-c", code, *args], cwd=REPO, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                     pass_fds=pass_fds)
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self._lines.put(line.strip())
        self._lines.put(None)

    def line(self, timeout: float = 60.0) -> str:
        got = self._lines.get(timeout=timeout)
        assert got is not None, "child exited without the expected line"
        return got

    def send(self, text: str = "go\n") -> None:
        self.proc.stdin.write(text)
        self.proc.stdin.flush()

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=30)
        self.proc.stdin.close()
        self.proc.stdout.close()


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


@pytest.fixture
def children():
    spawned: list[Child] = []
    yield spawned
    for child in spawned:
        child.kill()


def _spawn(children, code, home, *args) -> Child:
    child = Child(code, home, *args)
    children.append(child)
    return child


@pytest.mark.platforms("linux")
def test_simultaneous_cross_process_acquire_across_homes_has_exactly_one_winner(home, children):
    homes = [home, home / "profiles" / "alt", home, home / "profiles" / "alt"]
    procs = [_spawn(children, _HOLDER, h, "60") for h in homes]
    for p in procs:
        assert p.line() == "READY"
    for p in procs:
        p.send()
    results = [p.line() for p in procs]
    assert sorted(r.split()[0] for r in results) == ["BUSY", "BUSY", "BUSY", "OK"]
    winner = procs[[r.startswith("OK") for r in results].index(True)]
    episode = next(r.split()[1] for r in results if r.startswith("OK"))
    lease = cq.read_active()
    assert lease and lease.episode_id == episode and lease.producer_pid == winner.proc.pid
    for p in procs:
        p.send()
    assert [p.line() for p in procs] == ["CLOSED"] * 4
    assert cq.read_active() is None
    with pytest.raises(cq.QuietCooldownError):  # the winner's start consumed the 2h cooldown
        cq.acquire(max_s=30, reason="again")


@pytest.mark.platforms("linux")
def test_producer_crash_opens_admission_and_cooldown_survives(home, children):
    child = _spawn(children, _HOLDER, home, "300")
    assert child.line() == "READY"
    child.send()
    assert child.line().startswith("OK")
    assert cq.read_active() is not None and cq.inspect().state == "active"
    child.kill()  # SIGKILL: no cleanup path runs
    assert cq.read_active() is None
    reading = cq.inspect()
    assert reading.state == "inactive" and not reading.lease
    assert cq.quiet_paths().lease.exists(), "the reader leaves the dead producer's record untouched"
    with pytest.raises(cq.QuietCooldownError):
        cq.acquire(max_s=30, reason="after crash")


@pytest.mark.platforms("linux")
def test_clean_producer_exit_releases_and_removes_the_lease(home, children):
    child = _spawn(children, _HOLDER, home, "60")
    assert child.line() == "READY"
    child.send()
    assert child.line().startswith("OK")
    child.send()
    assert child.line() == "CLOSED"
    child.proc.wait(timeout=30)
    assert cq.read_active() is None and not cq.quiet_paths().lease.exists()


def _run_script(code: str, home: Path) -> list[str]:
    env = {**os.environ, "HERMES_HOME": str(home)}
    env.pop("PYTHONPATH", None)
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True,
                          timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


@pytest.mark.platforms("linux")
def test_raw_fork_child_closes_the_descriptor_without_unlocking_the_parent(home):
    assert _run_script(_FORK, home) == [
        "CHILD 0 1 0",  # child: fd already closed, owner closed, registry cleared
        "AFTER_CHILD_CLOSE active",  # the child's close() did not unlock or clear the parent's lease
        "AFTER_PARENT_CLOSE inactive",  # and the living child holds nothing that keeps the lock
    ]
    assert cq.read_active() is None


@pytest.mark.platforms("linux")
def test_faulty_inherited_descriptor_plus_producer_death_still_opens_admission(home, children):
    """A descendant keeping the lock descriptor cannot extend suppression, only deny a successor."""
    release_read, release_write = os.pipe()  # the faulty descendant exits when the test closes this
    child = Child(_FAULTY, home, str(release_read), pass_fds=(release_read,))
    children.append(child)
    os.close(release_read)
    producer = child
    _, _, holder_pid = producer.line().split()
    holder_pid = int(holder_pid)
    try:
        assert cq.read_active() is not None
        producer.kill()
        assert not _gone(holder_pid), "the faulty descendant outlives its producer"
        assert cq.read_active() is None
        assert cq.inspect().reason == "producer process is gone"
        with pytest.raises(cq.QuietBusyError, match="contended"):
            cq.acquire(max_s=30, reason="denied while the descendant holds the lock")
    finally:
        os.close(release_write)
        wait_until(lambda: _gone(holder_pid), what="faulty holder exit")
    with pytest.raises(cq.QuietCooldownError):  # lock is free again; the dead producer's start counts
        cq.acquire(max_s=30, reason="after holder exit")


# ---- platform capability ----------------------------------------------------------------------


@pytest.mark.platforms("linux")
def test_linux_uses_boottime_and_the_kernel_boot_id():
    import time

    monotonic = time.monotonic_ns()
    first = quiet_clock.suspend_inclusive_ns()
    second = quiet_clock.suspend_inclusive_ns()
    assert type(first) is int and second >= first
    assert first >= monotonic  # BOOTTIME includes suspend, so it is never behind MONOTONIC
    assert quiet_clock.boot_id() == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    assert quiet_clock.lease_supported()


@pytest.mark.platforms("linux")
def test_real_system_clock_round_trip(home):
    owner = cq.acquire(max_s=5, reason="real clock")
    assert cq.read_active() == owner.lease
    assert 0 < owner.lease.remaining_s(quiet_clock.suspend_inclusive_ns()) <= 5
    owner.close()
    assert cq.read_active() is None


@pytest.mark.platforms("macos")
def test_darwin_clock_and_boot_session_identity():
    """Runs only on a Mac; the pool never executes it, so Darwin behaviour is source-justified only."""
    first = quiet_clock.suspend_inclusive_ns()
    assert type(first) is int and quiet_clock.suspend_inclusive_ns() >= first
    assert quiet_clock.boot_id() and quiet_clock.lease_supported()
