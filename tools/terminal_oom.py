"""Name a foreground command the OOM killer took at the Kanban run's memory cap.

A Kanban run scope (``hermes-worker-kanban-<task>-run-<n>.scope``) runs with
``OOMPolicy=continue`` where systemd accepts it (``hermes_cli.kanban_worker_scope``),
so a command that fills the run's ``MemoryMax`` is killed alone and the agent keeps
going. A bare ``exit_code: 137`` then reads like any SIGKILL; this reads the scope's
``memory.events`` ``oom_kill`` count before and after the command and, when it rose,
says what happened and what to do instead. The kernel's record (``journalctl -k``) says
whose process died, so a concurrent job's kill is not pinned on the command.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Optional

_SCOPE_PREFIX = "hermes-worker-kanban-"
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC_CGROUP = Path("/proc/self/cgroup")
_SIGKILL_EXITS = {137, -9}
# A survivable command's own scope keeps systemd's default OOMPolicy=stop (process_registry): after
# an OOM kill there, systemd stops the scope with SIGTERM, so the command exits 143 / -15.
_SCOPE_STOP_EXITS = {143, -15}


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


def prefer_command_victim(argv: list[str]) -> list[str]:
    """Raise only the child's OOM priority, without preexec_fn in our threaded host."""
    if run_scope_cgroup() is not None and (choom := shutil.which("choom")):
        return [choom, "-n", "1000", "--", *argv]
    return argv


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
        return "unknown"
    return f"{int(raw) / (1024 * 1024):g} MiB" if raw.isdecimal() else raw


def _procs(cgroup: Optional[Path]) -> frozenset:
    """The pids in ``cgroup`` now (its ``cgroup.procs``), empty when unreadable."""
    if cgroup is None:
        return frozenset()
    try:
        return frozenset(int(p) for p in (cgroup / "cgroup.procs").read_text(encoding="utf-8-sig").split())
    except (OSError, ValueError):
        return frozenset()


def _kernel_victim(unit: str, since: float) -> Optional[dict]:
    """The kernel's last OOM victim in ``unit`` since ``since`` (None when unreadable)."""
    from hermes_cli.kanban_worker_scope import journal_since, kernel_oom_victim
    return kernel_oom_victim(unit, since=journal_since(since))


def _size(limit_bytes) -> str:
    return f"{limit_bytes / (1024 * 1024):g} MiB" if isinstance(limit_bytes, int) else "unknown"


_ADVICE = "run it under `systemd-run --user --scope -p MemoryMax=…` or reduce its footprint"


class OomWatch:
    """Snapshot before a foreground command; :meth:`note` after it ends.

    The run scope is shared with the agent and any background job, so its ``oom_kill`` count
    rising does not say whose process died. The kernel's record does: the note asserts the
    command was killed only when the victim is not a process that was already running when
    the command started; otherwise it names the victim and says no more. A ``survivable``
    foreground command runs in its own transient scope, where any victim is the command's.
    Cost: every survivable foreground command that finishes, rc 0 included, pays one
    ``journalctl -k`` read bounded by ``since``; a run-scope command reads it only when the
    scope's ``oom_kill`` count rose.
    Attribution is approximate: membership is judged against the ``cgroup.procs`` snapshot taken
    at the start, and the kernel lookup returns only the window's last victim, so a pid reused
    during the command or a second kill in the same window can be misattributed.
    """

    def __init__(self, survivable=None) -> None:
        self.since = time.time() - 1
        self.survivable = survivable
        self.cgroup = run_scope_cgroup()
        self.before = oom_kills(self.cgroup)
        self.procs = _procs(self.cgroup)

    def note(self, returncode) -> Optional[str]:
        """The model-facing note when an OOM kill hit the command's scope while it ran."""
        own = self._own_scope_note(returncode)
        if own or self.cgroup is None or self.before is None:
            return own
        after = oom_kills(self.cgroup)
        if after is None or after <= self.before:
            return None
        cap = _memory_max(self.cgroup)
        victim = _kernel_victim(self.cgroup.name, self.since)
        if victim is not None and victim["pid"] in self.procs:  # a process older than this command
            return (f"a process already running before this command ({victim['task']} pid {victim['pid']}) was "
                    f"killed by the OOM killer at the run's memory cap ({cap}) while it ran; a concurrent "
                    "command or background job shares the cap")
        named = f"; kernel victim: {victim['task']} pid {victim['pid']}" if victim else ""
        if returncode in _SIGKILL_EXITS:
            if victim is None:  # no kernel record: the count alone does not say whose process died
                return (f"exited by SIGKILL while the OOM killer fired at the run's memory cap ({cap}); it was "
                        "probably the victim, but a concurrent command or background job shares the cap; "
                        + _ADVICE)
            return f"killed by the OOM killer at the run's memory cap ({cap}{named}); {_ADVICE}"
        return (f"a process was killed by the OOM killer at the run's memory cap ({cap}{named}) while "
                "this command ran (a pipeline may hide its exit status); run memory-heavy steps "
                "under `systemd-run --user --scope -p MemoryMax=…` or reduce their footprint")

    def _own_scope_note(self, returncode) -> Optional[str]:
        """A survivable command's own scope holds only it: any OOM victim there is the command's.

        Its scope is collected on exit, so no ``oom_kill`` count is left to compare: every
        survivable foreground command that finishes, rc 0 included, pays one ``journalctl -k``
        read, bounded by ``since`` (the command's start). As in the run scope, only a kill exit
        gets the firm wording: SIGKILL, or SIGTERM from systemd stopping the scope under its
        default ``OOMPolicy=stop``. Any other rc (0, or a ``cmd | grep`` exiting 1) may be a
        pipeline hiding the victim's exit status.
        """
        session = getattr(self.survivable, "session", None)
        unit = getattr(session, "systemd_unit", None)
        if not isinstance(unit, str) or not unit or returncode is None:
            return None
        victim = _kernel_victim(unit, self.since)
        if victim is None:
            return None
        cap = f"{_size(victim.get('limit_bytes'))}; kernel victim: {victim['task']} pid {victim['pid']}"
        if returncode in _SIGKILL_EXITS | _SCOPE_STOP_EXITS:
            return f"killed by the OOM killer at its scope's memory cap ({cap}); {_ADVICE}"
        return (f"a process was killed by the OOM killer at its scope's memory cap ({cap}) while this "
                f"command ran (a pipeline may hide its exit status); {_ADVICE}")
