"""Per-file peak RSS for scripts/run_tests_parallel.py. [daveey]

Each per-file pytest child is reaped with ``os.wait4``, whose rusage ``ru_maxrss``
is the peak resident set of that child and of every descendant it reaped. The
summary prints the top 5 files and one ``⚠ high-memory test file`` line per file
over the threshold (warning only; the run's exit code is unchanged).

Why: tests/hermes_state/test_sidecar_keys_registry.py peaked at 1.58 GB per run
on the 16 GB Mac mini lane for 3 days and nothing in CI noticed
(t_01a19beb37e87be6). Env: ``HERMES_TEST_FILE_RSS_WARN_MB`` (default 512). A file
that needs the memory on purpose raises its own threshold with one line anywhere
in it: ``# run_tests_parallel: rss-warn-mb=<N>``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

DEFAULT_RSS_WARN_MB = 512.0
_MB = 1024 * 1024
_RSS_WARN_MARKER_RE = re.compile(
    r"^\s*#\s*run_tests_parallel:\s*rss-warn-mb\s*=\s*(\d+(?:\.\d+)?)\s*$", re.MULTILINE
)
# Peak RSS in bytes per test file (max over its attempts); absent = not measured.
PEAK_RSS: dict[Path, int] = {}
_peak_rss_lock = threading.Lock()


def maxrss_bytes(ru_maxrss: int, platform: str = sys.platform) -> int:
    """``ru_maxrss`` in bytes: macOS reports bytes, Linux (and other POSIX) KiB."""
    return int(ru_maxrss) if platform == "darwin" else int(ru_maxrss) * 1024


def rss_warn_mb(env: Optional[dict] = None) -> float:
    raw = (os.environ if env is None else env).get("HERMES_TEST_FILE_RSS_WARN_MB") or ""
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_RSS_WARN_MB
    return value if value > 0 else DEFAULT_RSS_WARN_MB


def file_rss_warn_mb(file: Path, default_mb: float) -> float:
    """The file's own ``rss-warn-mb`` marker if it has one, else ``default_mb``."""
    try:
        m = _RSS_WARN_MARKER_RE.search(Path(file).read_text(encoding="utf-8-sig", errors="replace"))
    except OSError:
        return default_mb
    return float(m.group(1)) if m else default_mb


def record_peak_rss(file: Path, rss_bytes: Optional[int]) -> None:
    if rss_bytes is None:
        return
    with _peak_rss_lock:
        PEAK_RSS[file] = max(rss_bytes, PEAK_RSS.get(file, 0))


def _rel(file: Path, repo_root: Path) -> str:
    try:
        return str(Path(file).resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(file)


def peak_rss_report(peaks: dict[Path, int], repo_root: Path, warn_mb: float, top: int = 5) -> list[str]:
    """Summary lines: the ``top`` peak-RSS files, then one warning per file over its threshold."""
    if not peaks:
        return []
    ranked = sorted(peaks.items(), key=lambda kv: kv[1], reverse=True)
    lines = [f"  Top {min(top, len(ranked))} peak RSS:"]
    lines += [f"    {rss / _MB:>7.0f} MB  {_rel(f, repo_root)}" for f, rss in ranked[:top]]
    for f, rss in ranked:
        limit = file_rss_warn_mb(f, warn_mb)
        if rss > limit * _MB:
            lines.append(
                f"  \u26a0 high-memory test file: {_rel(f, repo_root)} peaked at "
                f"{rss / _MB:.0f} MB RSS (> {limit:.0f} MB, HERMES_TEST_FILE_RSS_WARN_MB)"
            )
    return lines


def print_peak_rss_report(repo_root: Path) -> None:
    """Print the report for every file this run measured (nothing when none were)."""
    with _peak_rss_lock:
        peaks = dict(PEAK_RSS)
    for line in peak_rss_report(peaks, repo_root, rss_warn_mb()):
        print(line)


def reap_with_rusage(proc: subprocess.Popen) -> None:
    """Make ``proc`` reap itself with os.wait4 and keep the rusage on ``proc._hermes_rusage``.

    Patches the instance's CPython POSIX reaper (``_try_wait``, which every
    ``wait()``/``communicate()`` goes through) instead of subclassing Popen, so
    tests that swap ``subprocess.Popen`` for a fake keep working. No-op on
    Windows (no ``os.wait4``) or on a Popen without that reaper.
    """
    if not hasattr(os, "wait4") or not hasattr(proc, "_try_wait"):
        return

    def _try_wait(wait_flags):
        try:
            pid, sts, ru = os.wait4(proc.pid, wait_flags)
        except ChildProcessError:
            return (proc.pid, 0)
        if pid == proc.pid:
            proc._hermes_rusage = ru  # type: ignore[attr-defined]
        return (pid, sts)

    proc._try_wait = _try_wait  # type: ignore[method-assign]


def proc_peak_rss_bytes(proc: object) -> Optional[int]:
    ru = getattr(proc, "_hermes_rusage", None)
    return None if ru is None else maxrss_bytes(ru.ru_maxrss)
