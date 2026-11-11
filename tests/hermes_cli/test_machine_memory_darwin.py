"""ABI fixture and policy equivalence for the direct Darwin memory reader."""
import ctypes

import pytest

from hermes_cli import machine_memory_darwin as native
from hermes_cli.machine_metrics import _macos_mem_used_pct


class Kernel:
    def __init__(self):
        self.total = 16 * 1024 ** 3
        self.page = 16384
        self.free, self.inactive, self.purgeable, self.speculative = 131072, 131072, 1024, 65536
        self.calls = []
        self.failure = None

    def sysctlbyname(self, name, value, size, new, newlen):
        assert new is None and newlen == 0
        self.calls.append(name)
        value._obj.value = self.total if name == b"hw.memsize" else self.page
        if self.failure == (name, "size"):
            size._obj.value -= 1
        if self.failure == (name, "zero"):
            value._obj.value = 0
        return -1 if self.failure == name else 0

    def mach_host_self(self):
        self.calls.append("host")
        return 0 if self.failure == "null" else 123

    def host_page_size(self, host, page):
        assert host == 123
        self.calls.append("page")
        page._obj.value = 0 if self.failure == "zero_page" else self.page
        return 1 if self.failure == "page" else 0

    def host_statistics64(self, host, flavor, stats, count):
        assert host == 123 and flavor == 4 and count._obj.value == 40
        self.calls.append("stats")
        stats._obj.free, stats._obj.inactive = self.free, self.inactive
        stats._obj.purgeable, stats._obj.speculative = self.purgeable, self.speculative
        if isinstance(self.failure, int):
            count._obj.value = self.failure
        return 1 if self.failure == "stats" else 0

    def mach_port_deallocate(self, task, host):
        assert (task, host) == (7, 123)
        self.calls.append("release")
        return 1 if self.failure == "release" else 0

    def text(self):
        return (f"Mach Virtual Memory Statistics: (page size of {self.page} bytes)\n"
                f"Pages free: {self.free-self.speculative}.\nPages inactive: {self.inactive}.\n"
                f"Pages purgeable: {self.purgeable}.\nPages speculative: {self.speculative}.\n")


@pytest.fixture
def kernel(monkeypatch):
    k = Kernel()
    monkeypatch.setattr(native, "_library", lambda: (k, 7))
    return k


def test_native_abi():
    assert ctypes.sizeof(native._VMStatistics64) == 160
    assert native._VMStatistics64.purgeable.offset == 88
    assert native._VMStatistics64.speculative.offset == 92
    assert native._VMStatistics64.swapped.offset == 152


def test_same_input_policy_freshness_and_cleanup(kernel):
    for free, inactive, purgeable, speculative in [(131072, 131072, 1024, 65536), (65536, 0, 0, 0), (0, 0, 0, 0), (2**30, 0, 0, 0)]:
        kernel.free, kernel.inactive, kernel.purgeable, kernel.speculative = free, inactive, purgeable, speculative
        assert native.read_used_pct() == _macos_mem_used_pct(kernel.total, kernel.text())
    assert kernel.calls == [b"hw.memsize", "host", "page", "stats", "release"] * 4


@pytest.mark.parametrize("failure", [b"hw.memsize", (b"hw.memsize", "size"), (b"hw.memsize", "zero"), "null", "page", "zero_page", "stats", 23, 41, "release"])
def test_fail_closed_and_no_port_leak(kernel, failure):
    kernel.failure = failure
    with pytest.raises((OSError, ValueError)):
        native.read_used_pct()
    assert kernel.calls.count("release") == int("page" in kernel.calls)


def test_old_kernel_prefix_is_sufficient(kernel):
    kernel.failure = 24
    assert native.read_used_pct() == _macos_mem_used_pct(kernel.total, kernel.text())


@pytest.mark.platforms("macos")
def test_missing_library_omits_without_spawning(monkeypatch):
    from hermes_cli import machine_metrics as mm

    def unavailable():
        raise OSError("libSystem unavailable")

    monkeypatch.setattr(native, "_library", unavailable)
    assert mm._mem_used_pct() is None


@pytest.mark.parametrize("error", [AttributeError("missing symbol"), ctypes.ArgumentError("bad ABI")])
def test_binding_incompatibility_is_unavailable(monkeypatch, error):
    def unavailable():
        raise error

    monkeypatch.setattr(native, "_library", unavailable)
    with pytest.raises(OSError, match="incompatible Darwin memory API"):
        native.read_used_pct()


def test_captured_vm_stat_policy_and_no_spawn(kernel, monkeypatch):
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("native reader spawned")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    # Numeric memory-only excerpt captured from a real arm64 vm_stat snapshot.
    text = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free: 450707.
Pages inactive: 3251273.
Pages speculative: 303400.
Pages purgeable: 106035.
"""
    kernel.total = 137438953472
    kernel.free, kernel.inactive, kernel.speculative, kernel.purgeable = 754107, 3251273, 303400, 106035
    assert native.read_used_pct() == _macos_mem_used_pct(kernel.total, text) == 51.0
    # A different process map page size must not affect host counters.
    monkeypatch.setattr("os.sysconf", lambda name: 4096)
    assert native.read_used_pct() == 51.0
