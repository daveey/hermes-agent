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
    # An "unfenced" intent (hermes-sync on a host with no launcher gate, e.g. the hub) only tells the gateway
    # that its restart belongs to a core update. No launcher parks on it, so the update must still verify
    # its own gateway restart.
    if isinstance(data, dict) and data.get("kind") == "unfenced":
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


def parked_launchd_launcher(entry: dict) -> bool:
    """Prove a launchd PID is only a launcher parked at this updater's ancestor fence.

    A socket answer or any launcher child means the gateway may still be running.
    Never exempt an unresponsive gateway merely because an intent exists.
    """
    if entry.get("kind") != "launchd" or not entry.get("home"):
        return False
    home = Path(entry["home"])
    owner = ancestor_fence_owner([home])
    if owner is None:
        return False
    try:
        import psutil
    except ImportError:
        return False
    try:
        from gateway.control_socket import query_gateway_control
        from gateway.status import get_process_start_time
        from hermes_cli.gateway_launchd import _SIGNED_WRAPPER_SOURCE, gateway_launcher_path

        if query_gateway_control(home, "identify", timeout=0.5) is not None:
            return False
        root = psutil.Process(entry["pid"])
        if not root.is_running():
            return False
        if entry.get("ct") is not None and get_process_start_time(entry["pid"]) != entry["ct"]:
            return False

        def launcher(proc):
            argv = proc.cmdline()
            # Direct executable or interpreter + script, never a substring in a shim's -c source.
            offset = next((i for i in range(min(2, len(argv)))
                           if Path(argv[i]).name == "hermes-gateway-launcher"), None)
            return (offset is not None and len(argv) > offset + 4
                    and Path(argv[offset]).resolve() == gateway_launcher_path(str(home)).resolve()
                    and argv[offset + 1] == "--home"
                    and Path(argv[offset + 2]).resolve() == home.resolve()
                    and argv[offset + 3] == "--")

        candidate = root
        if not launcher(root):
            argv = root.cmdline()
            app_bin = Path.home() / "Applications/Hermes Python.app/Contents/MacOS"
            if (len(argv) != 5 or argv[1:4] != ["-I", "-c", _SIGNED_WRAPPER_SOURCE]
                    or Path(argv[0]).parent != app_bin or not Path(argv[0]).name.startswith("python3")
                    or not argv[4].startswith("exec ")):
                return False
            children = root.children()
            if len(children) != 1:
                return False
            candidate = children[0]  # signed Python's forwarding shim directly owns the launcher
        return bool(launcher(candidate) and candidate.is_running() and not candidate.children()
                    and ancestor_fence_owner([home]) == owner)
    except (ImportError, OSError, ValueError, KeyError, TypeError, psutil.Error):
        return False


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
