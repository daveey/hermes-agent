"""Read-only scratch deletion veto for externally supervised, card-owned jobs.

The external-job convention is HERMES_OWNER_CARD=<card>@<hostname>. A marker
file or a stored PID is not authority: inspect a same-user process's current cwd,
exact owner tag and start identity on every cleanup attempt. Never signal it.
Existing GC retries after exit; no retention setting or permanent marker changes.
This deliberately covers cwd dependencies, not arbitrary absolute output paths.
"""
from __future__ import annotations

import logging
import re
import stat
import time
from pathlib import Path

import psutil

from gateway.status import get_process_start_time
from hermes_cli.kanban_host_identity import hostname_prefix

log = logging.getLogger(__name__)
_SCAN_SECONDS = 1.0


def _local_owner_tag(tag: str, hostname: str) -> bool:
    # The job may belong to a child or another card sharing this directory.
    # Its live cwd, not equality with the row being cleaned, establishes use.
    match = re.fullmatch(r't_[0-9a-f]+@([A-Za-z0-9.-]+)', tag)
    return bool(match and match[1].lower().split('.')[0] == hostname.lower().split('.')[0])


def _holds_workspace(proc, path: Path, hostname: str, username: str) -> bool:
    """Unknown identity after a matching tag conservatively keeps this attempt."""
    depends_on_path = False
    try:
        if proc.username() != username:
            return False
        birth = get_process_start_time(proc.pid)
        cwd = proc.cwd()
        if not cwd or not Path(cwd).is_relative_to(path):
            return False
        depends_on_path = True
        if not _local_owner_tag(proc.environ().get('HERMES_OWNER_CARD', ''), hostname):
            return False
        if proc.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
            return False
        current = get_process_start_time(proc.pid)
        # A known replacement is not the process whose authority we observed.
        return birth is None or current is None or current == birth
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError):
        # Only a demonstrated cwd dependency earns an uncertainty veto. A root
        # service we cannot even inspect must not pin every task on the machine.
        return depends_on_path


def defer_owned_job_cleanup(task_id: str, path: Path | str) -> bool:
    """True means keep scratch this pass; all four removal paths use this veto.

    A bounded incomplete scan fails closed. No cached verdict: a stale tag or
    exited PID cannot retain a directory on the next successful GC pass.
    An explicit ``kanban.scratch_gc_preserve`` pin vetoes first. [daveey]
    """
    from hermes_cli.kanban_scratch_preserve import preserved
    if preserved(task_id, path):
        log.info('Deferring scratch cleanup for %s: pinned by kanban.scratch_gc_preserve', task_id)
        return True
    path = Path(path)
    try:
        if not stat.S_ISDIR(path.lstat().st_mode):
            return True
        path = path.resolve(strict=True)
        hostname = hostname_prefix()[:-1]
        username = psutil.Process().username()
        deadline = time.monotonic() + _SCAN_SECONDS
        for proc in psutil.process_iter():
            if time.monotonic() >= deadline:
                log.warning('Deferring scratch cleanup for %s: owned-job scan budget exhausted', task_id)
                return True
            if _holds_workspace(proc, path, hostname, username):
                log.info('Deferring scratch cleanup for %s: live/uncertain owned job pid=%s cwd under %s',
                         task_id, proc.pid, path)
                return True
    except (psutil.Error, OSError, RuntimeError):
        log.info('Deferring scratch cleanup for %s: owned-job scan unavailable', task_id)
        return True
    return False
