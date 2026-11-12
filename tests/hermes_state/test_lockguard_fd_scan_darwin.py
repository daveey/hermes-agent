"""Darwin must not retain the per-descriptor GIL handoff fallback."""
import os
import sys

import pytest

import hermes_state_lockguard_fdscan as fdscan

pytestmark = pytest.mark.platforms("macos")


def test_darwin_native_identity_and_complete_enumeration(tmp_path):
    assert fdscan.native_enabled(), "Darwin must use the verified GIL-held scanner"
    path = tmp_path / "regular"
    path.write_bytes(b"x")
    fds = [os.open(path, os.O_RDONLY), os.open(os.devnull, os.O_RDONLY),
           os.open(tmp_path, os.O_RDONLY), *os.pipe()]
    try:
        scanned = dict(fdscan.open_fd_identities())
        for fd in fds:
            st = os.fstat(fd)
            assert fdscan.identity(fd) == (st.st_dev, st.st_ino)
            assert scanned[fd] == (st.st_dev, st.st_ino)
    finally:
        for fd in fds:
            os.close(fd)
    assert fdscan.identity(-1) is None


def test_darwin_saturated_buffer_uses_complete_fallback(monkeypatch):
    import hermes_state_lockguard_darwin as darwin
    import hermes_state_lockguard as guard

    assert fdscan.native_enabled()
    fd = os.open(os.devnull, os.O_RDONLY)
    try:
        st = os.fstat(fd)
        monkeypatch.setattr(darwin, "MAX_FDS", 1)
        assert fdscan.open_fd_identities() is None
        assert (fd, (st.st_dev, st.st_ino)) in list(guard._own_fds_for({(st.st_dev, st.st_ino)}))
    finally:
        os.close(fd)


def test_darwin_scan_under_cpu_contention(tmp_path):
    from tests.hermes_state.test_lockguard_fd_scan_gil import (
        test_own_fds_scan_stays_fast_while_another_thread_is_cpu_bound as check,
    )

    assert fdscan.native_enabled()
    check(tmp_path)


def test_high_descriptor_survives_lowered_soft_limit(tmp_path):
    # Isolate the process-wide limit: no other pytest thread sees the change.
    import subprocess
    import textwrap

    code = textwrap.dedent('''
        import fcntl, os, resource
        import hermes_state_lockguard_fdscan as scan
        low = os.open(os.devnull, os.O_RDONLY)
        high = fcntl.fcntl(low, fcntl.F_DUPFD, 1024)
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
            st = os.fstat(high)
            assert scan.native_enabled()
            assert dict(scan.open_fd_identities())[high] == (st.st_dev, st.st_ino)
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
            os.close(high)
            os.close(low)
    ''')
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr


def test_probe_open_failure_disarms(monkeypatch):
    import hermes_state_lockguard_darwin as darwin

    def unavailable(*args):
        raise OSError("descriptor limit")

    monkeypatch.setattr(darwin, "_open", unavailable)  # the module seam, never the shared os.open
    assert darwin.build() is None
