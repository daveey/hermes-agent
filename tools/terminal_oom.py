"""Name a foreground command the OOM killer took at the Kanban run's memory cap.

A Kanban run scope (``hermes-worker-kanban-<task>-run-<n>.scope``) runs with
``OOMPolicy=continue`` where systemd accepts it (``hermes_cli.kanban_worker_scope``),
so a command that fills the run's ``MemoryMax`` is killed alone and the agent keeps
going. A bare ``exit_code: 137`` then reads like any SIGKILL; this reads the scope's
``memory.events`` ``oom_kill`` count before and after the command and, when it rose,
says what happened and what to do instead.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

_SCOPE_PREFIX = "hermes-worker-kanban-"
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC_CGROUP = Path("/proc/self/cgroup")
_SIGKILL_EXITS = {137, -9}


def run_scope_cgroup() -> Optional[Path]:
    """This process's cgroup when it is a Kanban run scope, else None."""
    try:
        for line in _PROC_CGROUP.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("0::"):
                relative = line[3:].strip()
                name = relative.rsplit("/", 1)[-1]
                if name.startswith(_SCOPE_PREFIX) and name.endswith(".scope"):
                    return _CGROUP_ROOT / relative.lstrip("/")
                return None
    except OSError:
        pass
    return None


def oom_kills(cgroup: Optional[Path]) -> Optional[int]:
    """The scope's ``memory.events`` ``oom_kill`` count, or None when unreadable."""
    if cgroup is None:
        return None
    try:
        for line in (cgroup / "memory.events").read_text(encoding="utf-8-sig").splitlines():
            key, _, value = line.partition(" ")
            if key == "oom_kill":
                return int(value)
    except (OSError, ValueError):
        pass
    return None


def _memory_max(cgroup: Path) -> str:
    try:
        raw = (cgroup / "memory.max").read_text(encoding="utf-8-sig").strip()
    except OSError:
        return "MemoryMax unknown"
    return f"{int(raw) / (1024 * 1024):g} MiB" if raw.isdecimal() else raw


class OomWatch:
    """Snapshot before a foreground command; :meth:`note` after it ends."""

    def __init__(self) -> None:
        self.cgroup = run_scope_cgroup()
        self.before = oom_kills(self.cgroup)

    def note(self, returncode) -> Optional[str]:
        """The model-facing note when the run scope's OOM killer fired during the command."""
        if self.cgroup is None or self.before is None:
            return None
        after = oom_kills(self.cgroup)
        if after is None or after <= self.before:
            return None
        cap = _memory_max(self.cgroup)
        if returncode in _SIGKILL_EXITS:
            return (f"killed by the OOM killer at the run's memory cap ({cap}); run it under "
                    "`systemd-run --user --scope -p MemoryMax=…` or reduce its footprint")
        return (f"a process was killed by the OOM killer at the run's memory cap ({cap}) while "
                "this command ran (a pipeline may hide its exit status); run memory-heavy steps "
                "under `systemd-run --user --scope -p MemoryMax=…` or reduce their footprint")
