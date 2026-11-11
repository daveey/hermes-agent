"""Read Darwin memory counters without forking the multithreaded gateway.

ABI: XNU mach/vm_statistics.h (vm_statistics64, revision 2) and host_info.h.
vm_stat displays free_count - speculative_count, so the existing reader's
free + inactive + speculative + purgeable equals free_count + inactive_count
+ purgeable_count here. Keep that policy, not psutil's different definition.
"""
from __future__ import annotations

import ctypes
from functools import lru_cache


class _VMStatistics64(ctypes.Structure):
    _fields_ = (
        [(n, ctypes.c_uint32) for n in ("free", "active", "inactive", "wired")]
        + [(n, ctypes.c_uint64) for n in (
            "zero_fill", "reactivations", "pageins", "pageouts", "faults", "cow_faults", "lookups", "hits", "purges")]
        + [(n, ctypes.c_uint32) for n in ("purgeable", "speculative")]
        + [(n, ctypes.c_uint64) for n in ("decompressions", "compressions", "swapins", "swapouts")]
        + [(n, ctypes.c_uint32) for n in ("compressor", "throttled", "external", "internal")]
        + [(n, ctypes.c_uint64) for n in ("total_uncompressed", "swapped")]
    )


@lru_cache(maxsize=1)
def _library():
    # Cache bindings only. Every read gets fresh counters; no timer-based value cache.
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    lib.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                                ctypes.c_void_p, ctypes.c_size_t]
    lib.sysctlbyname.restype = ctypes.c_int
    lib.mach_host_self.argtypes = []
    lib.mach_host_self.restype = ctypes.c_uint32
    lib.host_page_size.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_size_t)]
    lib.host_page_size.restype = ctypes.c_int
    lib.host_statistics64.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p,
                                      ctypes.POINTER(ctypes.c_uint32)]
    lib.host_statistics64.restype = ctypes.c_int
    lib.mach_port_deallocate.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    lib.mach_port_deallocate.restype = ctypes.c_int
    return lib, ctypes.c_uint32.in_dll(lib, "mach_task_self_").value


def _sysctl_uint(lib, name: bytes, kind):
    value = kind()
    size = ctypes.c_size_t(ctypes.sizeof(value))
    if lib.sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "memory sysctl failed")
    if size.value != ctypes.sizeof(value) or value.value <= 0:
        raise ValueError("invalid memory sysctl size/value")
    return value.value


def read_used_pct() -> float:
    try:
        return _read_used_pct()
    except (AttributeError, ctypes.ArgumentError) as exc:
        raise OSError("incompatible Darwin memory API") from exc


def _read_used_pct() -> float:
    lib, task = _library()
    total = _sysctl_uint(lib, b"hw.memsize", ctypes.c_uint64)
    host = lib.mach_host_self()
    if not host:
        raise OSError("null host port")
    try:
        # Host counters use kernel pages, not a translated process's map page size.
        page = ctypes.c_size_t()
        if lib.host_page_size(host, ctypes.byref(page)) != 0 or not page.value:
            raise OSError("host_page_size failed")
        page_size = page.value
        stats = _VMStatistics64()
        count = ctypes.c_uint32(ctypes.sizeof(stats) // ctypes.sizeof(ctypes.c_uint32))
        if lib.host_statistics64(host, 4, ctypes.byref(stats), ctypes.byref(count)) != 0:
            raise OSError("host_statistics64 failed")
        # Revision 0 contains all policy inputs (24 natural_t words); accept newer/older kernels.
        if not 24 <= count.value <= ctypes.sizeof(stats) // 4:
            raise ValueError("short/oversized host statistics")
        available = (stats.free + stats.inactive + stats.purgeable) * page_size
        return round(max(0.0, min(100.0, 100.0 * (total - available) / total)), 1)
    finally:
        if lib.mach_port_deallocate(task, host) != 0:
            raise OSError("host port deallocation failed")
