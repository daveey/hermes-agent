"""Measured CPU busy for the hub admission gate (worker-first overflow, docs/worker-first-v1.md).

Busy is computed from two aggregate ``/proc/stat`` snapshots taken about one second apart on the monotonic clock:
``100 * (Δtotal - Δidle - Δiowait - Δnice) / Δtotal`` where total sums user..steal. Positive-nice user time is
excluded so low-priority batch work does not veto foreground admission; system/IRQ/steal remain busy.
This is aggregate non-nice busy, not a per-process nice>=10 filter. ``guest``/``guest_nice`` are already
included in user/nice and are never added again; ``steal`` counts as busy (time this host could not use). Any read
error, malformed line, counter decrease (reboot/reset), non-positive total, or an elapsed window outside
[1000, 2000] ms yields ``None`` (unknown), which every caller treats as "refuse", never as idle.

There is no daemon or background sampler. Two callers sample on demand:
- the metrics reporter, only on a host whose installation-root config sets
  ``kanban.admission.cpu_busy_below_pct`` (the Board uses the reported triple for hub overflow), and
- the final claim gate in ``kanban_admission.claim_transaction``, with an independent full sample taken while
  ``admission.lock`` is held.

Normalized load average is NOT a substitute for this measurement anywhere in the gate.
"""
from __future__ import annotations

import math
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

WINDOW_S = 1.0
MIN_WINDOW_MS = 1000
MAX_WINDOW_MS = 2000
CONFIG_KEY = ("kanban", "admission", "cpu_busy_below_pct")
FIELDS = ("cpu_busy_pct", "cpu_busy_window_ms", "cpu_busy_sample_end_ms")


def parse_proc_stat(text: str) -> Optional[tuple[int, int]]:
    """``(total, idle_like)`` from the aggregate ``cpu`` line; ``None`` when it is missing or malformed."""
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] != "cpu":
            continue
        if len(parts) < 9:
            return None
        try:
            user, nice, system, idle, iowait, irq, softirq, steal = (int(v) for v in parts[1:9])
        except ValueError:
            return None
        values = (user, nice, system, idle, iowait, irq, softirq, steal)
        if any(v < 0 for v in values):
            return None
        return sum(values), idle + iowait + nice
    return None


def busy_pct(before: Optional[tuple[int, int]], after: Optional[tuple[int, int]]) -> Optional[float]:
    """Unrounded busy percentage between two snapshots, or ``None`` when the delta is not a valid measurement."""
    if before is None or after is None:
        return None
    d_total, d_idle = after[0] - before[0], after[1] - before[1]
    if d_total <= 0 or d_idle < 0 or d_idle > d_total:
        return None
    return 100.0 * (d_total - d_idle) / d_total


def _read_proc_stat() -> str:
    return Path("/proc/stat").read_text(encoding="ascii")


def sample(window_s: float = WINDOW_S, *, read: Callable[[], str] = _read_proc_stat,
           clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
           sleep: Callable[[float], None] = time.sleep, linux: Optional[bool] = None) -> Optional[dict[str, Any]]:
    """One complete measurement ``{cpu_busy_pct, cpu_busy_window_ms, cpu_busy_sample_end_ms}`` or ``None``.

    Blocks for ``window_s`` (about 1 s). The window is measured on the monotonic clock; the end stamp is the
    producer's wall clock in ms (the Board checks it against its own receipt time). Non-Linux hosts report ``None``.
    """
    if linux is None:
        linux = sys.platform.startswith("linux")
    if not linux:
        return None
    try:
        t0 = clock()
        first = parse_proc_stat(read())
        sleep(window_s)
        second = parse_proc_stat(read())
        t1 = clock()
        end_ms = int(wall() * 1000)
    except (OSError, UnicodeError, ValueError):
        return None
    window_ms = (t1 - t0) * 1000.0
    if not math.isfinite(window_ms) or not MIN_WINDOW_MS <= round(window_ms, 6) <= MAX_WINDOW_MS:
        return None
    pct = busy_pct(first, second)
    if pct is None:
        return None
    return {"cpu_busy_pct": pct, "cpu_busy_window_ms": round(window_ms), "cpu_busy_sample_end_ms": end_ms}


def _is_number(v: Any) -> bool:
    return type(v) in (int, float) and math.isfinite(v)


def valid_limit(limit: Any) -> bool:
    """A configured threshold must be a finite number in (0, 100]."""
    return _is_number(limit) and 0 < limit <= 100


def below(pct: Any, limit: Any) -> bool:
    """Strictly ``pct < limit``, no rounding. Missing, non-finite or out-of-range values never admit."""
    return valid_limit(limit) and _is_number(pct) and 0 <= pct <= 100 and pct < limit


OFF = "off"
ON = "on"
UNKNOWN = "unknown"


def root_limit(read_doc: Optional[Callable[[Path], Any]] = None, root: Optional[Path] = None) -> tuple[str, Optional[float]]:
    """The installation-root ``kanban.admission.cpu_busy_below_pct`` (never a profile override).

    ``(OFF, None)`` when the root config or the key is absent; ``(ON, limit)`` for a valid value;
    ``(UNKNOWN, None)`` when the config cannot be read or the value is invalid. UNKNOWN refuses admission:
    an operator who wrote a gate must not lose it to a typo or an unreadable file.
    """
    if root is None:
        from hermes_constants import get_default_hermes_root
        root = Path(get_default_hermes_root())
    if read_doc is None:
        from hermes_cli.config_backend import read_config_doc_readonly as read_doc
    try:
        doc = read_doc(Path(root) / "config.yaml")
    except FileNotFoundError:
        return OFF, None
    except Exception:  # health: allow BLE001 -- any unreadable/unparsable root config is "unknown", which refuses
        return UNKNOWN, None
    node: Any = doc
    for key in CONFIG_KEY:
        if node is None:
            return OFF, None
        if not isinstance(node, dict):
            return UNKNOWN, None
        if key not in node:
            return OFF, None
        node = node[key]
    if node is None:
        return OFF, None
    return (ON, float(node)) if valid_limit(node) else (UNKNOWN, None)
