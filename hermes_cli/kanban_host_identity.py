"""Restart- and network-stable macOS claim ownership (not a PID identity).

macOS can change gethostname() on wake/network changes. A boot-session UUID is
shared by all dispatchers on this host and survives app/gateway restarts, but
cannot cause us to signal a PID on another host or after reboot. Other platforms
retain the existing hostname contract. Legacy hostname claims remain readable.
"""
from functools import lru_cache
import socket
import subprocess
import sys
import uuid


def hostname_prefix() -> str:
    try:
        return f"{socket.gethostname() or 'unknown'}:"
    except OSError:
        return "unknown:"


@lru_cache(maxsize=1)
def boot_prefix() -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2, check=True,
        )
        return f"boot-{uuid.UUID(result.stdout.strip())}:"
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def claim_prefix() -> str:
    return boot_prefix() or hostname_prefix()


def is_local_claim(lock: str | None, prefix: str | None = None) -> bool:
    if prefix is None:
        from hermes_cli import kanban_db
        prefix = kanban_db._host_prefix()
    if not lock:
        return False
    if lock.startswith(prefix):
        return True
    # Compatibility with workers launched before the stable prefix was deployed.
    # Never accept an arbitrary foreign hostname merely because its PID exists.
    return bool(prefix.startswith("boot-") and lock.startswith(hostname_prefix()))
