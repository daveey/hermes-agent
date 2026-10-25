"""Retain failed worker scopes until their cause is durably booked by the reaper.

Only Kanban opts out of --collect. Successful scopes still collect automatically;
failed scopes retain systemd's Result/MemoryPeak even after their cgroup is gone.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

SNAPSHOT_PREFIX = "[kanban-worker-scope] "


def emit_exit_snapshot() -> None:
    """Save cgroup peak before a SIGTERM-handling worker exits and systemd removes it.

    Older systemd versions retain Result but not MemoryPeak. No snapshot is
    possible when the worker itself takes SIGKILL; that peak stays unknown.
    """
    task_id = os.environ.get("HERMES_KANBAN_TASK", "")
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "")
    if not task_id or not run_id.isdecimal():
        return
    unit = f"hermes-worker-kanban-{task_id}-run-{run_id}.scope"
    try:
        relative = next(line[3:] for line in Path("/proc/self/cgroup").read_text(encoding="utf-8-sig").splitlines()
                        if line.startswith("0::") and line.endswith("/" + unit))
        cgroup = Path("/sys/fs/cgroup") / relative.lstrip("/")
        values: dict = {key: _bytes((cgroup / filename).read_text(encoding="utf-8-sig").strip()) for key, filename in (
            ("memory_max_bytes", "memory.max"), ("memory_peak_bytes", "memory.peak"))}
        events = dict(line.split() for line in (cgroup / "memory.events").read_text(encoding="utf-8-sig").splitlines())
        values.update(systemd_unit=unit, oom_kill=int(events.get("oom_kill", "0")))
        print(SNAPSHOT_PREFIX + json.dumps(values), file=sys.stderr, flush=True)
    except (OSError, ValueError, StopIteration):
        return


def _logged_snapshot(log: str, unit: str) -> dict:
    for line in reversed(log.splitlines()):
        if not line.startswith(SNAPSHOT_PREFIX):
            continue
        try:
            data = json.loads(line[len(SNAPSHOT_PREFIX):])
            if not isinstance(data, dict) or data.get("systemd_unit") != unit:
                continue
            return {key: value for key, value in data.items()
                    if key in {"memory_max_bytes", "memory_peak_bytes", "oom_kill"}
                    and type(value) is int and 0 <= value < (1 << 64) - 1}
        except ValueError:
            continue
    return {}


def retain_failed_scope(argv: list[str]) -> list[str]:
    """Remove systemd-run's --collect, never a worker argument of the same name."""
    if not argv or Path(argv[0]).name != "systemd-run" or "--scope" not in argv or "--" not in argv:
        return argv
    boundary = argv.index("--")
    return [arg for arg in argv[:boundary] if arg != "--collect"] + argv[boundary:]


def _systemctl(*args: str) -> subprocess.CompletedProcess | None:
    from tools.process_registry import systemd_user_bus_env

    env = systemd_user_bus_env()
    if env is None:
        return None
    try:
        return subprocess.run(
            ["systemctl", "--user", *args], env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Non-systemd hosts, unavailable bus, or old systemd: preserve exit-code handling.
        return None


def read_failure(task_id: str, run_id: int | None, log: str = "") -> dict | None:
    if run_id is None:
        return None
    unit = f"hermes-worker-kanban-{task_id}-run-{run_id}.scope"
    proc = _systemctl("show", unit, "-p", "LoadState", "-p", "Result",
                      "-p", "MemoryMax", "-p", "MemoryPeak")
    props = (dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
             if proc is not None and not proc.returncode else {})
    snapshot = _logged_snapshot(log, unit)
    result = props.get("Result") if props.get("LoadState") == "loaded" else None
    if snapshot.get("oom_kill", 0) > 0:
        result = "oom-kill"
    if result not in {"oom-kill", "signal"}:
        return None
    return {
        "systemd_unit": unit, "scope_result": result,
        "memory_max_bytes": snapshot.get("memory_max_bytes", _bytes(props.get("MemoryMax", ""))),
        "memory_peak_bytes": snapshot.get("memory_peak_bytes", _bytes(props.get("MemoryPeak", ""))),
    }


def _bytes(raw: str) -> int | None:
    # UINT64_MAX is systemd's unset sentinel; don't report an exabyte-scale peak.
    return int(raw) if raw.isdecimal() and int(raw) < (1 << 64) - 1 else None


def _size(value: int | None) -> str:
    return f"{value / (1024 * 1024):g} MiB" if value is not None else "unknown"


def describe(failure: dict) -> str:
    limit = _size(failure["memory_max_bytes"])
    peak = _size(failure["memory_peak_bytes"])
    if failure["scope_result"] == "oom-kill":
        return (f"Worker was OOM-killed at {limit} (peak {peak}); run memory-heavy steps "
                "under `systemd-run --user --scope -p MemoryMax=…` or reduce the footprint.")
    return f"Worker scope died from a signal (MemoryMax {limit}; peak {peak})."


def release_failure(metadata: dict) -> None:
    """Called only after run/error accounting commits; reset-failed allows scope GC."""
    unit = metadata.get("systemd_unit")
    if unit:
        _systemctl("reset-failed", unit)
