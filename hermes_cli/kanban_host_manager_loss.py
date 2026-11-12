"""Root-attested loss of a worker host's systemd user manager. [daveey]

2026-11-12 02:34Z and 03:03Z, hermes-worker-1: ``user@1000.service`` was SIGKILLed twice (a test
fixture's ``killpg(1, 9)`` broadcast). Every card in flight both times was charged by the
``owner_death_repeat`` crash-loop guard, although for the bystanders the host, not the card, failed.

The worker's root ``hermes-worker-recovery`` watchdog (hermes-sync ``state``:
``hosts/hermes-workers/bin/worker_manager_loss.py``) snapshots the healthy gateway's live ancestry to
the user manager and, before restarting a SIGKILLed manager, writes a ``manager_loss`` record into its
root-owned incident receipt when the retained ExecMain data matches that snapshot. This module reads
those receipts and answers one question: did THIS owner (exact pid + boot-relative birth, this boot,
this host) die with a user manager that was SIGKILLed?

That is operational cohort evidence, NOT proof of who sent the signal: a card can still kill its own
manager. ``kanban_owner_death`` therefore waives at most :data:`MAX_WAIVERS` repeats per card.
Anything missing, unreadable, writable by a non-root user, oversized, from another boot/host, or not
naming this owner exactly returns ``None`` (charged as before).
"""
from __future__ import annotations

import json
import math
import os
import socket
import stat
import sys
import time
from pathlib import Path
from typing import Optional

RECOVERY_DIR = Path("/var/lib/hermes-worker-recovery")
MAX_RECEIPT_BYTES = 8192
RECEIPT_SCAN_LIMIT = 16
MAX_COHORT_AGE = 60  # seconds between the healthy snapshot and the manager's exit (producer bound)
BIRTH_TOLERANCE = 0.005  # half a 100 Hz clock tick: births are kernel start ticks / CLK_TCK
MAX_WAIVERS = 1  # repeats a card may have waived for host manager loss (scan window of its runs)
MANAGER_UNIT = "user@1000.service"


def _number(value) -> bool:
    """A real finite number: an int too large for a float (``float(10**400)`` raises OverflowError)
    or an inf/nan is no evidence."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _trusted_node(path: Path, *, uid: int, directory: bool) -> bool:
    """``path`` is a non-symlink regular file/dir owned by ``uid`` that no other user can write."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    kind = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
    if not kind or st.st_uid != uid or st.st_mode & 0o022:
        return False
    return directory or st.st_size <= MAX_RECEIPT_BYTES


def trusted_tree(root: Path, *, uid: int = 0) -> bool:
    """``root`` and every ancestor are root-owned, non-symlink and not group/other-writable."""
    path = Path(os.path.abspath(root))
    for node in (path, *path.parents):
        if not _trusted_node(node, uid=uid, directory=True):
            return False
    return True


def _read(path: Path, uid: int) -> Optional[dict]:
    if not _trusted_node(path, uid=uid, directory=False):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if st.st_uid != uid or st.st_mode & 0o022 or not stat.S_ISREG(st.st_mode):
                return None
            raw = fh.read(MAX_RECEIPT_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_RECEIPT_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _candidates(root: Path, uid: int) -> list[Path]:
    paths = [root / "status.json"]
    incidents = root / "incidents"
    if _trusted_node(incidents, uid=uid, directory=True):
        try:
            entries = [(e.stat(follow_symlinks=False).st_mtime, Path(e.path)) for e in os.scandir(incidents)
                       if e.name.endswith(".json")]
        except OSError:
            entries = []
        entries.sort(reverse=True)
        paths += [p for _, p in entries[:RECEIPT_SCAN_LIMIT]]
    return paths


def _matches(data: dict, *, owner_pid: int, birth: float, boot_id: str, host: str, now: float) -> bool:
    loss = data.get("manager_loss")
    if not isinstance(loss, dict) or data.get("unit") != MANAGER_UNIT or data.get("machine") != host:
        return False
    owner, manager = loss.get("owner"), loss.get("manager")
    if not isinstance(owner, dict) or not isinstance(manager, dict):
        return False
    observed, ended, owner_birth = loss.get("observed_monotonic"), loss.get("exit_monotonic"), owner.get("birth")
    if not (_number(observed) and _number(ended) and _number(owner_birth)):
        return False
    observed, ended, owner_birth = float(observed), float(ended), float(owner_birth)  # type: ignore[arg-type]
    return (loss.get("schema") == 1 and loss.get("boot_id") == boot_id and loss.get("host") == host
            and loss.get("unit") == MANAGER_UNIT and loss.get("exit_code") == "killed"
            and loss.get("exit_status") == 9 and type(owner.get("pid")) is int and owner["pid"] == owner_pid
            and abs(owner_birth - birth) <= BIRTH_TOLERANCE
            and owner_birth <= observed <= ended <= now and ended - observed <= MAX_COHORT_AGE)


def manager_loss_incident(receipt: dict, *, root: Path = RECOVERY_DIR, uid: int = 0,
                          boot_id: Optional[str] = None, host: Optional[str] = None,
                          now: Optional[float] = None, check_ancestors: bool = True) -> Optional[str]:
    """The root incident id attesting that ``receipt``'s owner died with its SIGKILLed user manager.

    ``receipt`` is the run's ``owner_admitted`` payload (``pid`` + boot-relative ``birth``). Linux only;
    every failure is ``None``.
    """
    if not sys.platform.startswith("linux") and boot_id is None:
        return None
    pid, birth = receipt.get("pid"), receipt.get("birth")
    if type(pid) is not int or pid <= 1 or not _number(birth):
        return None
    birth = float(birth)  # type: ignore[arg-type]
    try:
        if boot_id is None:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        host = host if host is not None else socket.gethostname()
    except OSError:
        return None
    now = time.monotonic() if now is None else now
    if check_ancestors and not trusted_tree(root, uid=uid):
        return None
    if not _trusted_node(root, uid=uid, directory=True):
        return None
    for path in _candidates(root, uid):
        data = _read(path, uid)
        if data and _matches(data, owner_pid=pid, birth=birth, boot_id=boot_id, host=host, now=now):
            incident = data.get("incident_id")
            if isinstance(incident, str) and 0 < len(incident) <= 64:
                return incident
    return None
