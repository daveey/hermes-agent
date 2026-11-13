"""Suspend-inclusive elapsed clock and boot identity for the claim-quiet lease.

``time.monotonic()`` is not usable for a lease deadline: CPython 3.14 implements it on macOS with
``mach_absolute_time()`` (``Python/pytime.c``, ``implementation = "mach_absolute_time()"``), which
stops while the machine sleeps, so a lease could outlive its wall budget. The kernel clocks used here
keep counting through sleep:

* Linux: ``CLOCK_BOOTTIME`` (``clock_gettime(2)``: like ``CLOCK_MONOTONIC`` but includes suspend).
* Darwin: ``CLOCK_MONOTONIC_RAW``. ``clock_gettime(3)`` on macOS defines ``CLOCK_UPTIME_RAW`` as
  "the same manner as ``CLOCK_MONOTONIC_RAW``, but does not increment while the system is asleep"
  (and equal to ``mach_absolute_time()``), i.e. ``CLOCK_MONOTONIC_RAW`` is the sleep-inclusive
  ``mach_continuous_time()`` clock and is unaffected by NTP slewing. This is justified from those
  published sources and CPython's ``time`` exposure of the constant; it has NOT been executed on a Mac.

Anything else answers ``None``: an unknown clock means no lease and no measurement, never a longer one.
Boot identity is the kernel boot id (Linux) or ``kern.bootsessionuuid`` (Darwin).
"""

from __future__ import annotations

import functools
import math
import sys
import time
from pathlib import Path
from typing import Any

from hermes_platform.host.facts import _sysctl_str, os_family

MAX_BOOT_ID_CHARS = 128


class ClockSampleError(ValueError):
    """A sampled clock value is missing or not the exact type/range the lease logic requires."""


def validate_wall(value: Any) -> float:
    """Finite, non-negative real wall time; ``bool`` and strings are not numbers here."""
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ClockSampleError(f"wall clock sample is not a finite non-negative number: {value!r}")
    return float(value)


def validate_elapsed_ns(value: Any) -> int:
    """Exact non-negative ``int`` nanoseconds (never ``bool``, float, NaN or ``None``)."""
    if type(value) is not int or value < 0:
        raise ClockSampleError(f"elapsed clock sample is not a non-negative integer: {value!r}")
    return value


def validate_boot_id(value: Any) -> str:
    if type(value) is not str or not value or len(value) > MAX_BOOT_ID_CHARS or not value.isprintable():
        raise ClockSampleError("boot identity is not bounded printable text")
    return value


def lease_supported() -> bool:
    """Whether this host can hold the POSIX ``flock`` lifetime lease at all."""
    if os_family() not in ("linux", "darwin"):
        return False
    try:
        import fcntl
    except ImportError:
        return False
    return suspend_inclusive_ns() is not None


def _clock_id() -> int | None:
    name = "CLOCK_BOOTTIME" if sys.platform.startswith("linux") else (
        "CLOCK_MONOTONIC_RAW" if sys.platform == "darwin" else "")
    return getattr(time, name, None) if name else None


def suspend_inclusive_ns() -> int | None:
    """Nanoseconds on the suspend-inclusive clock, or ``None`` when this host has none."""
    clock_id = _clock_id()
    if clock_id is None:
        return None
    try:
        return time.clock_gettime_ns(clock_id)
    except OSError:
        return None


@functools.cache
def boot_id() -> str | None:
    """Identity of the running boot, or ``None`` when the host cannot name it."""
    if sys.platform.startswith("linux"):
        try:
            text = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return None
    elif sys.platform == "darwin":
        try:
            text = _sysctl_str(b"kern.bootsessionuuid")
        except OSError:
            return None
    else:
        return None
    return text if text and len(text) <= MAX_BOOT_ID_CHARS and text.isprintable() else None


def wall_time() -> float:
    return time.time()
