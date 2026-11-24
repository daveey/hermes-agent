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

linux_native = pytest.mark.skipif(not sys.platform.startswith("linux") or not fdscan.native_enabled(),
                                  reason="Linux fd-table sweep")


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
    monkeypatch.setattr(fdscan, "_darwin_descriptors", None)
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


def test_stranded_sidecar_scan_stays_inside_the_reader_timeout(tmp_path):
    """``os.readlink`` and ``os.path.realpath`` release the GIL. Under a busy interpreter each
    re-acquire waits a switch interval, so ``stranded_sidecars`` (called from ``recover`` while
    the board lifecycle lock is held) stretched a sub-millisecond walk into seconds and every
    reader raised ``BoardLifecycleBusy``. The call must finish inside the 0.5s reader timeout.
    """
    if not sys.platform.startswith("linux"):
        pytest.skip("procfs sidecar scan")
    from hermes_cli import kanban_lockguard
    # Production precondition: the only lock holder, ``kanban_lockguard.connect``, imports
    # sqlite_safe_read (and the native scanner) BEFORE entering lifecycle_access
    # (test_connect_warms_scan_imports_before_the_lifecycle_lock). Establish that here so the
    # bound measures the walk, not a first-time module import that the lock never covers.
    import hermes_cli.sqlite_safe_read  # fdscan is imported at module top

    wal = tmp_path / "board.db-wal"
    held = os.open(wal, os.O_RDWR | os.O_CREAT, 0o600)
    os.unlink(wal)  # still open: readlink reports " (deleted)"
    filler = [os.open(os.devnull, os.O_RDONLY) for _ in range(400)]
    stop = threading.Event()

    def spin():
        while not stop.is_set():
            sum(range(80))

    spinners = [threading.Thread(target=spin, daemon=True) for _ in range(24)]
    old = sys.getswitchinterval()
    sys.setswitchinterval(0.005)
    for spinner in spinners:
        spinner.start()
    try:
        started = time.perf_counter()
        # Scan on this thread: it already holds the GIL, so the measurement is the walk,
        # not how long a fresh thread waits to be scheduled behind the spinners.
        found = kanban_lockguard.stranded_sidecars(tmp_path / "board.db")
        elapsed = time.perf_counter() - started
    finally:
        stop.set()
        sys.setswitchinterval(old)
        for spinner in spinners:
            spinner.join(2)
        os.close(held)
        for fd in filler:
            os.close(fd)
    assert elapsed < 0.5, f"stranded scan took {elapsed:.3f}s; reader timeout is 0.5s"
    assert any(str(wal) in item for item in found)


def test_connect_warms_scan_imports_before_the_lifecycle_lock(tmp_path):
    """The bound above assumes the scan's modules are loaded before the lock is taken. A cold
    import under the lock (seconds under 24 spinners) would reopen BoardLifecycleBusy, so pin it:
    in a fresh interpreter, ``connect`` must have both modules loaded when it enters
    ``lifecycle_access``."""
    import subprocess
    import textwrap

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent(f"""
        import contextlib, sys
        from hermes_cli import kanban_generation, kanban_lockguard
        for name in ("hermes_cli.sqlite_safe_read",):
            assert name not in sys.modules, name + " already loaded; probe is not cold"
        real = kanban_generation.lifecycle_access
        seen = []

        @contextlib.contextmanager
        def spy(database, timeout):
            seen.append(sorted(m for m in ("hermes_cli.sqlite_safe_read", "hermes_state_lockguard_fdscan")
                               if m in sys.modules))
            with real(database, timeout) as lock:
                yield lock

        kanban_generation.lifecycle_access = spy
        kanban_lockguard.connect({str(tmp_path / 'board.db')!r}).close()
        print(seen)
    """))
    env = {**os.environ, "PYTHONPATH": root + os.pathsep + os.environ.get("PYTHONPATH", "")}
    out = subprocess.run([sys.executable, str(script)], cwd=root, env=env, capture_output=True,
                         text=True, timeout=60, check=False)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[['hermes_cli.sqlite_safe_read', 'hermes_state_lockguard_fdscan']]", out.stdout


def test_held_realpath_matches_os_path_realpath(tmp_path, monkeypatch):
    from hermes_state_lockguard_fdscan import held_realpath

    if held_realpath("/tmp") is None:
        pytest.skip("native realpath off")
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    missing = tmp_path / "no-such-board.db"
    monkeypatch.chdir(tmp_path)
    for path in (str(tmp_path), str(missing), str(link / "no-such-board.db"), "kanban.db"):
        assert held_realpath(path) == os.path.realpath(path)
