"""Host-wide wake instant in the awake clock domain, independent of process age.

Darwin's IOPMrootDomain updates kern.wake_abs_time with clock_get_uptime after
CPU resume (including dark wakes). Unlike kern.waketime, this is not calendar
time: converting Mach ticks gives the same domain as Python's monotonic clock.
Other hosts retain the caller's per-process suspend detection.
"""

from __future__ import annotations

import ctypes
import functools
import sys


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


@functools.cache
def _darwin_api():
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    lib.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    lib.sysctlbyname.restype = ctypes.c_int
    lib.mach_timebase_info.argtypes = [ctypes.POINTER(_Timebase)]
    lib.mach_timebase_info.restype = ctypes.c_int
    return lib


def _darwin_wake_awake_s(lib) -> float | None:
    ticks = ctypes.c_uint64()
    size = ctypes.c_size_t(ctypes.sizeof(ticks))
    if lib.sysctlbyname(b"kern.wake_abs_time", ctypes.byref(ticks),
                        ctypes.byref(size), None, 0) != 0:
        return None
    if size.value != ctypes.sizeof(ticks) or ticks.value == 0:
        return None
    scale = _Timebase()
    if lib.mach_timebase_info(ctypes.byref(scale)) != 0:
        return None
    if scale.numer == 0 or scale.denom == 0:
        return None
    return ticks.value * scale.numer / scale.denom / 1e9


def host_wake_awake_s() -> float | None:
    """Last native wake in monotonic seconds, or None if no trustworthy signal exists."""
    if sys.platform != "darwin":
        return None
    try:
        return _darwin_wake_awake_s(_darwin_api())
    except (OSError, AttributeError):
        return None
