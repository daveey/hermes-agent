"""A persist_on_release job leaves its spawner's systemd user unit. [daveey]

A Kanban worker runs in ``hermes-worker-kanban-<task>-run-<n>.scope`` (OOMPolicy=stop,
KillMode=control-group), which systemd stops when the run OOMs and the dispatcher stops after the
run ends. A ``persist_on_release`` job spawned inside it died with the worker (a paid-rental
watchdog on the hub, 2026-11-03), so it gets its own transient scope in the same
aggregate-bounded ``hermes-kanban.slice``, with the run scope's own memory cap.

Any other user unit kills it the same way: Desktop's hub backend runs in
``hermes-always-on-dashboard.service`` (KillMode=control-group), which every hub update restarts, so
a persist job started from a hub chat died with it although Hermes itself spares persist jobs on
``backend_shutdown`` (a CI supervisor, twice on 2026-11-04). Such a job gets its own scope in
``app.slice``, bounded like its spawner's unit (uncapped when that unit is). A process in no user unit
(system service, container root, a login session scope) or with no user bus is unchanged.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

KANBAN_RUN_SCOPE_PREFIX = "hermes-worker-kanban-"
KANBAN_SLICE = "hermes-kanban.slice"
APP_SLICE = "app.slice"
PROC_SELF_CGROUP = "/proc/self/cgroup"
CGROUP_ROOT = Path("/sys/fs/cgroup")


def _own_cgroup(proc_cgroup: Optional[str]) -> Optional[str]:
    """This process's cgroup-v2 path (the ``0::`` line), without the leading slash."""
    try:
        for line in Path(proc_cgroup or PROC_SELF_CGROUP).read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("0::"):
                return line[3:].strip().lstrip("/")
    except OSError:
        pass
    return None


def kanban_run_scope_cgroup(proc_cgroup: Optional[str] = None) -> Optional[Path]:
    """This process's Kanban run-scope cgroup directory, or None outside one."""
    relative = _own_cgroup(proc_cgroup) or ""
    name = relative.rsplit("/", 1)[-1]
    if name.startswith(KANBAN_RUN_SCOPE_PREFIX) and name.endswith(".scope"):
        return CGROUP_ROOT / relative
    return None


def user_unit_cgroup(proc_cgroup: Optional[str] = None) -> Optional[Path]:
    """This process's systemd *user* unit cgroup directory (a ``.service``/``.scope`` below
    ``user@<uid>.service``), or None when it runs in no user unit."""
    relative = _own_cgroup(proc_cgroup) or ""
    parts = relative.split("/")
    manager = next((i for i, part in enumerate(parts) if part.startswith("user@") and part.endswith(".service")),
                   None)
    if manager is None or manager == len(parts) - 1 or not parts[-1].endswith((".service", ".scope")):
        return None
    return CGROUP_ROOT / relative


def _unit_memory_max(unit: Path) -> str:
    try:
        return (unit / "memory.max").read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def persisted_job_scope_argv(session, shell_argv: list[str], unit_suffix: str) -> list[str]:
    """``systemd-run --user --scope`` argv moving a persisted job out of this process's systemd user unit
    (and naming ``session.systemd_unit``); ``shell_argv`` itself (the same object) when that does not apply."""
    from tools import process_registry as pr

    if not (pr._IS_LINUX and session.persist_on_release):
        return shell_argv
    run_scope = kanban_run_scope_cgroup()
    unit = run_scope if run_scope is not None else user_unit_cgroup()
    binary = shutil.which("systemd-run") if unit is not None else None
    if unit is None or binary is None or not pr._systemd_run_user_scope_available():
        return shell_argv
    raw = _unit_memory_max(unit)
    capped = raw.isdigit() and int(raw) >= pr._MIN_WORKER_MEMORY_MAX_BYTES
    memory_max: int | str
    if run_scope is not None:
        memory_max, slice_name = (int(raw) if capped else 0), KANBAN_SLICE
    else:
        # The spawner's own bound; the 4 GiB worker default would OOM a build its uncapped spawner can run.
        memory_max, slice_name = (int(raw) if capped else "infinity"), APP_SLICE
    session.systemd_unit = f"hermes-worker-{unit_suffix}.scope"
    return pr._systemd_scope_argv(binary, f"hermes-worker-{unit_suffix}", *shell_argv,
                                  memory_max=memory_max, slice_name=slice_name)
