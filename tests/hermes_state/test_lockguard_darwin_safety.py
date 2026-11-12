"""Exercise Darwin ABI refusal with injected native-call failures."""
import ctypes
import struct
from types import SimpleNamespace

import pytest

import hermes_state_lockguard_darwin as darwin

pytestmark = pytest.mark.platforms("macos")


class Function:
    def __init__(self, fn):
        self.fn = fn

    def __call__(self, *args):
        return self.fn(*args)


@pytest.fixture
def abi(monkeypatch):
    state = SimpleNamespace(dev=-7, ino=123, listed=8, closes=[], stat_dev=-7)

    def fstat(fd, buf):
        if fd < 0:
            return -1
        struct.pack_into("=iHHQ", buf, 0, state.dev, 0, 0, state.ino)
        return 0

    def pidinfo(pid, flavor, arg, buf, size):
        assert flavor == 1 and arg == 0
        struct.pack_into("=iI", buf, 0, 42, 1)
        return state.listed

    libc = SimpleNamespace(fstat=Function(fstat), proc_pidinfo=Function(pidinfo))
    monkeypatch.setattr(darwin.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(ctypes, "PyDLL", lambda _: libc)
    monkeypatch.setattr(darwin.os, "open", lambda *args: 42)
    monkeypatch.setattr(darwin.os, "close", state.closes.append)
    monkeypatch.setattr(darwin.os, "fstat", lambda fd: SimpleNamespace(st_dev=state.stat_dev, st_ino=123))
    state.libc = libc
    return state


@pytest.mark.parametrize("unsigned", [False, True])
def test_runtime_dev_conversion(abi, unsigned):
    abi.stat_dev = ((1 << 64) - 7) if unsigned else -7
    native, descriptors = darwin.build()
    assert native(42) == (abi.stat_dev, 123)
    assert descriptors() == [42]
    assert native(-1) is None
    assert native(1 << 31) is None
    assert abi.closes == [42]


@pytest.mark.parametrize("size", [-1, 0, 1, 7, 9, darwin.MAX_FDS * 8, darwin.MAX_FDS * 8 + 8])
def test_invalid_enumeration_disarms_at_probe_and_runtime(abi, size):
    _native, descriptors = darwin.build()
    abi.listed = size
    assert descriptors() is None
    assert darwin.build() is None
    assert abi.closes == [42, 42]


@pytest.mark.parametrize("unsigned", [False, True])
def test_nonnegative_sentinel_learns_conversion_at_runtime(abi, monkeypatch, unsigned):
    # devfs numbering is host-specific: a positive /dev/null device must not disarm.
    abi.dev = abi.stat_dev = 7
    native, descriptors = darwin.build()
    assert native(42) == (7, 123)
    abi.dev = -7
    abi.stat_dev = ((1 << 64) - 7) if unsigned else -7
    assert native(42) == (abi.stat_dev, 123)

    def fail(fd):
        raise AssertionError("conversion is learned; no Python fstat handoff")

    monkeypatch.setattr(darwin.os, "fstat", fail)
    assert native(42) == (abi.stat_dev, 123)
    assert descriptors() == [42]


def test_unlearned_negative_device_defers_to_python(abi, monkeypatch):
    abi.dev = abi.stat_dev = 7
    native, _descriptors = darwin.build()
    abi.dev, abi.stat_dev = -7, 99  # inconsistent: report Python's identity, learn nothing
    assert native(42) == (99, 123)
    abi.stat_dev = -7  # consistent signed answer: learned
    assert native(42) == (-7, 123)
    abi.dev, abi.stat_dev = -8, 12345  # ignored now; native signed conversion is used
    assert native(42) == (-8, 123)


def test_runtime_fstat_failure_before_learning_reports_closed(abi, monkeypatch):
    abi.dev = abi.stat_dev = 7
    native, _descriptors = darwin.build()
    abi.dev = -7

    def closed(fd):
        raise OSError(9, "closed")

    monkeypatch.setattr(darwin.os, "fstat", closed)
    assert native(42) is None


@pytest.mark.parametrize("dev,stat_dev,ino", [(7, 8, 123), (7, 7, 999), (-7, 99, 123), (-7, -7, 999)])
def test_unproven_identity_disarms(abi, dev, stat_dev, ino):
    abi.dev, abi.stat_dev, abi.ino = dev, stat_dev, ino
    assert darwin.build() is None
    assert abi.closes == [42]


def test_missing_symbol_disarms(abi):
    del abi.libc.proc_pidinfo
    assert darwin.build() is None
    assert abi.closes == []


def test_unsupported_architecture_disarms(abi, monkeypatch):
    monkeypatch.setattr(darwin.platform, "machine", lambda: "unknown")
    assert darwin.build() is None
    assert abi.closes == []


def test_probe_not_listed_disarms(abi, monkeypatch):
    monkeypatch.setattr(darwin.os, "open", lambda *args: 43)
    assert darwin.build() is None
    assert abi.closes == [43]
