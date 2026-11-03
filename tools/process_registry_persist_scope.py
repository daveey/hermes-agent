"""A Kanban worker's persist_on_release job leaves the worker's run scope. [daveey]

A Kanban worker runs in ``hermes-worker-kanban-<task>-run-<n>.scope`` (OOMPolicy=stop,
KillMode=control-group), which systemd stops when the run OOMs and the dispatcher stops after the
run ends. A ``persist_on_release`` job spawned inside it died with the worker (a paid-rental
watchdog on the hub, 2026-11-03), so it gets its own transient scope in the same
aggregate-bounded ``hermes-kanban.slice``, with the run scope's own memory cap.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

KANBAN_RUN_SCOPE_PREFIX = "hermes-worker-kanban-"
KANBAN_SLICE = "hermes-kanban.slice"


def kanban_run_scope_cgroup(proc_cgroup: str = "/proc/self/cgroup") -> Optional[Path]:
    """This process's Kanban run-scope cgroup directory, or None outside one."""
    try:
        for line in Path(proc_cgroup).read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("0::"):
                relative = line[3:].strip()
                name = relative.rsplit("/", 1)[-1]
                if name.startswith(KANBAN_RUN_SCOPE_PREFIX) and name.endswith(".scope"):
                    return Path("/sys/fs/cgroup") / relative.lstrip("/")
                return None
    except OSError:
        pass
    return None


def persisted_job_scope_argv(session, shell_argv: list[str], unit_suffix: str) -> list[str]:
    """``systemd-run --user --scope`` argv moving a persisted job out of this process's Kanban run scope
    (and naming ``session.systemd_unit``); ``shell_argv`` itself (the same object) when that does not apply."""
    from tools import process_registry as pr

    run_scope = kanban_run_scope_cgroup() if pr._IS_LINUX and session.persist_on_release else None
    binary = shutil.which("systemd-run") if run_scope is not None else None
    if binary is None or not pr._systemd_run_user_scope_available():
        return shell_argv
    try:
        raw = (run_scope / "memory.max").read_text(encoding="utf-8-sig").strip()
    except OSError:
        raw = ""
    memory_max = int(raw) if raw.isdigit() and int(raw) >= pr._MIN_WORKER_MEMORY_MAX_BYTES else 0
    session.systemd_unit = f"hermes-worker-{unit_suffix}.scope"
    return pr._systemd_scope_argv(binary, f"hermes-worker-{unit_suffix}", *shell_argv,
                                  memory_max=memory_max, slice_name=KANBAN_SLICE)
