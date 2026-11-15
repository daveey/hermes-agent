"""Native ABI decoding uses a fake sysctl; platform dispatch runs on its real host."""

import ctypes

import pytest

from hermes_platform import wake_clock


class FakeLib:
    def __init__(self, *, ticks=12_000_000_000, size=8, status=0,
                 numer=125, denom=3, scale_status=0):
        self.ticks, self.size, self.status = ticks, size, status
        self.numer, self.denom, self.scale_status = numer, denom, scale_status

    def sysctlbyname(self, name, out, size, new, length):
        assert name == b"kern.wake_abs_time"
        assert new is None and length == 0
        ctypes.cast(out, ctypes.POINTER(ctypes.c_uint64))[0] = self.ticks
        ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))[0] = self.size
        return self.status

    def mach_timebase_info(self, out):
        scale = ctypes.cast(out, ctypes.POINTER(wake_clock._Timebase)).contents
        scale.numer, scale.denom = self.numer, self.denom
        return self.scale_status


def test_native_ticks_convert_to_monotonic_seconds():
    assert wake_clock._darwin_wake_awake_s(FakeLib()) == 500.0


@pytest.mark.parametrize("overrides", [
    {"ticks": 0}, {"size": 4}, {"status": -1},
    {"scale_status": 1}, {"numer": 0}, {"denom": 0},
])
def test_missing_or_invalid_native_signal(overrides):
    assert wake_clock._darwin_wake_awake_s(FakeLib(**overrides)) is None


@pytest.mark.platforms("not macos")
def test_unsupported_host_has_no_native_signal():
    assert wake_clock.host_wake_awake_s() is None


@pytest.mark.platforms("macos")
def test_darwin_entry_uses_native_reader(monkeypatch):
    monkeypatch.setattr(wake_clock, "_darwin_api", lambda: FakeLib())
    assert wake_clock.host_wake_awake_s() == 500.0


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("error", [OSError, AttributeError])
def test_unavailable_native_api_is_no_signal(monkeypatch, error):
    def unavailable():
        raise error("unavailable")
    monkeypatch.setattr(wake_clock, "_darwin_api", unavailable)
    assert wake_clock.host_wake_awake_s() is None
