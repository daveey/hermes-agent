"""The WAL lock guard's descriptor scan never hands the GIL off per descriptor. [daveey]

``hold()`` runs on every guarded SQLite open and fstat-ed every descriptor in the process. With
``os.fstat`` each call released and re-took the GIL, so any CPU-bound thread made the scan wait out
the switch interval once per descriptor. On the hub (628 fds, 134 threads) one Kanban board open
held the board's lifecycle lock for over 500 ms, and readers on every board failed with "board
lifecycle is busy" in bursts.
"""

import os
import sys
import threading
import time

import pytest

import hermes_state_lockguard as guard
import hermes_state_lockguard_fdscan as fdscan

linux_native = pytest.mark.skipif(not fdscan.native_enabled(), reason="native fstat only on Linux x86_64/aarch64")


@linux_native
def test_native_identity_matches_os_fstat(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"x")
    fds = [os.open(p, os.O_RDONLY), os.open(os.devnull, os.O_RDONLY), os.open(tmp_path, os.O_RDONLY)]
    try:
        for fd in fds:
            st = os.fstat(fd)
            assert fdscan.identity(fd) == (st.st_dev, st.st_ino)
    finally:
        for fd in fds:
            os.close(fd)
    closed = os.open(os.devnull, os.O_RDONLY)
    os.close(closed)
    assert fdscan.identity(closed) is None
    assert fdscan.identity(-1) is None


def test_fallback_identity_when_native_is_off(monkeypatch, tmp_path):
    monkeypatch.setattr(fdscan, "_native", None)
    fd = os.open(os.devnull, os.O_RDONLY)
    try:
        st = os.fstat(fd)
        assert fdscan.identity(fd) == (st.st_dev, st.st_ino)
    finally:
        os.close(fd)
    assert fdscan.identity(fd) is None


@linux_native
def test_sweep_finds_exactly_the_listed_descriptors(tmp_path):
    extra = [os.open(os.devnull, os.O_RDONLY) for _ in range(50)]
    try:
        listed = {}
        for name in os.listdir("/proc/self/fd"):
            ident = fdscan._os_identity(int(name))
            if ident is not None:  # listdir's own directory fd is already closed here
                listed[int(name)] = ident
        swept = dict(fdscan.open_fd_identities() or [])
        assert swept == listed
        assert set(extra) <= set(swept)
    finally:
        for fd in extra:
            os.close(fd)


def test_oversized_fd_table_falls_back_to_the_listing(monkeypatch):
    monkeypatch.setattr(fdscan, "_fd_table_size", lambda: fdscan.MAX_NATIVE_SWEEP + 1)
    assert fdscan.open_fd_identities() is None
    fd = os.open(os.devnull, os.O_RDONLY)
    try:
        st = os.fstat(fd)
        assert (fd, (st.st_dev, st.st_ino)) in list(guard._own_fds_for({(st.st_dev, st.st_ino)}))
    finally:
        os.close(fd)


@linux_native
def test_own_fds_scan_stays_fast_while_another_thread_is_cpu_bound(tmp_path):
    """Hundreds of descriptors plus one spinning thread: the scan must not wait once per fd."""
    if (fdscan._fd_table_size() or 0) > fdscan.MAX_NATIVE_SWEEP:
        pytest.skip("fd table already above the native sweep cap in this process")
    target = tmp_path / "board.db"
    target.write_bytes(b"x")
    mine = os.open(target, os.O_RDONLY)
    filler = [os.open(os.devnull, os.O_RDONLY) for _ in range(600)]
    st = os.fstat(mine)
    stop = threading.Event()

    def spin():
        while not stop.is_set():
            sum(range(1000))

    spinner = threading.Thread(target=spin, daemon=True)
    spinner.start()
    old = sys.getswitchinterval()
    sys.setswitchinterval(0.005)  # CPython's default; pinned so the bound means the same everywhere
    try:
        worst = 0.0
        for _ in range(3):
            t = time.perf_counter()
            found = list(guard._own_fds_for({(st.st_dev, st.st_ino)}))
            worst = max(worst, time.perf_counter() - t)
            assert (mine, (st.st_dev, st.st_ino)) in found
    finally:
        sys.setswitchinterval(old)
        stop.set()
        spinner.join(5)
        for fd in (mine, *filler):
            os.close(fd)
    # Per-fd hand-off: ~600 x 5 ms = seconds. GIL-held sweep: a few switch intervals at most.
    assert worst < 0.1, f"scan took {worst:.3f}s with one busy thread"
