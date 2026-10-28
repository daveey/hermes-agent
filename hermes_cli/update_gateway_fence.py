"""[daveey] Is this update running inside a core-update launcher fence?

hermes-sync applies a core update as ``hermes update --yes`` INSIDE a fence (state repo
``hermes-sync`` ``fenced_core_update``, docs/core-update-safety.md): it writes
``<gateway home>/core-update/intent`` with its own pid and holds LOCK_EX on ``exec.lock``. Every
``hermes-gateway-launcher`` parks while that intent exists, so NO launcher-supervised gateway can
start until the fence owner releases it — which happens only after this update process exits.

The post-update fleet probe therefore cannot see a successor from inside the fence: it polled the
full 120 s settle window, found no rows and recorded a false ``gateway_restart`` follow-up on every
fenced update (David-MBP, 5 consecutive updates on 2026-10-27/28). Verifying the restart is the
fence owner's job once it releases the fence; this update keeps the fleet obligation armed so a
gateway that never comes back still warns at the next CLI start.

Only an intent written by a live ANCESTOR of this process counts: that is the one fence that is
provably held for this update's whole lifetime. An intent from an unrelated (or dead) process is
not evidence of anything here, and the normal fail-closed verification applies.
"""
from __future__ import annotations

import json
import os
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Iterable, Optional


def _ancestor_pids() -> set[int]:
    """Pids of every ancestor of this process (not including itself). Empty when unreadable."""
    with suppress(Exception):
        import psutil

        return {proc.pid for proc in psutil.Process(os.getpid()).parents()}
    pids: set[int] = set()
    pid = os.getppid()
    for _ in range(64):
        if pid <= 1 or pid in pids:
            break
        pids.add(pid)
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, encoding="utf-8",
                                 timeout=5, check=False).stdout.strip()
            pid = int(out)
        except (OSError, ValueError, subprocess.SubprocessError):
            break
    return pids


def _intent_pid(home: Path) -> Optional[int]:
    try:
        data = json.loads((Path(home) / "core-update" / "intent").read_text(encoding="utf-8-sig"))
        pid = int(data["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return pid if pid > 1 else None


def _gateway_homes() -> list[Path]:
    homes: list[Path] = []
    with suppress(Exception):
        from hermes_constants import get_default_hermes_root

        homes.append(Path(get_default_hermes_root()))
    with suppress(Exception):
        from hermes_cli.update_receipt import _profile_homes

        homes.extend(home for _profile, home in _profile_homes())
    seen: set[str] = set()
    unique: list[Path] = []
    for home in homes:
        key = os.path.realpath(home)
        if key not in seen:
            seen.add(key)
            unique.append(home)
    return unique


def ancestor_fence_owner(homes: Optional[Iterable[Path]] = None) -> Optional[int]:
    """Pid of a live ancestor of this process holding a core-update intent on a gateway home, else None.

    Never raises: any unreadable state answers None, which keeps the normal fail-closed verification.
    """
    try:
        candidates = list(homes) if homes is not None else _gateway_homes()
        pids = {pid for pid in (_intent_pid(home) for home in candidates) if pid is not None}
        if not pids:
            return None
        ancestors = _ancestor_pids()
        owners = sorted(pids & ancestors)
        return owners[0] if owners else None
    except Exception:  # health: allow BLE001 -- detection only; unknown keeps fail-closed verification
        return None
