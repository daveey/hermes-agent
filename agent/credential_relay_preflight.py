"""Bounded, fail-open Anthropic relay catch-up before a pinned request. [daveey]

Only a producer's successful provider receipt proves freshness; auth.json mtime
also changes on pool persistence. The existing relay lock serializes processes,
including 401 recovery. Never call this while holding a pool/auth-store lock.
"""
from __future__ import annotations

import json
import logging
import math
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_WAIT_S = 30.0
_POLL_S = 0.1


def _receipt(path: Path, provider: str) -> tuple[float, float] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or data.get("version") != 1:
            return None
        providers = data.get("providers")
        row = providers.get(provider) if isinstance(providers, dict) else None
        if not isinstance(row, dict):
            return None
        values = (row.get("checked_at"), row.get("interval_s"))
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values):
            return None
        return values
    except (OSError, ValueError, OverflowError):
        return None


def _stale(receipt: tuple[float, float]) -> bool:
    checked_at, interval = receipt
    return time.time() - checked_at > 2 * interval


def _wait(deadline: float) -> bool:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    time.sleep(min(_POLL_S, remaining))
    return True


def _claim_attempt(lock: Any, provider: str, cooldown: float, checked_at: float) -> bool:
    """Bound repeated failures across sessions without pretending a pull succeeded."""
    lock.seek(0)
    try:
        attempts = json.load(lock)
    except ValueError:
        attempts = {}
    if not isinstance(attempts, dict):
        attempts = {}
    last = attempts.get(provider)
    count = 0
    if isinstance(last, dict):
        previous = last.get("checked_at")
        count = last.get("count", 0)
        count = min(16, max(0, count)) if type(count) is int else 0
        if not isinstance(previous, (int, float)) or isinstance(previous, bool) or not math.isfinite(previous) or checked_at > previous:
            last, count = None, 0
        else:
            last = last.get("attempted_at")
    now = time.time()
    delay = min(3600.0, cooldown * 2 ** max(0, count - 1))
    if isinstance(last, (int, float)) and not isinstance(last, bool) and 0 <= now - last < delay:
        return False
    # Count attempts against the same successful receipt, never command exits.
    # A newer receipt resets this even if it aged before the next request.
    attempts[provider] = {"attempted_at": now, "checked_at": checked_at, "count": min(16, count + 1)}
    lock.seek(0)
    lock.truncate()
    json.dump(attempts, lock)
    lock.flush()
    return True


def _pull_locked(cmd: list[str], stamp: Path, provider: str, deadline: float, lock: Any) -> None:
    """Recheck after lock acquisition; wait for async launchd apply, not kickstart exit."""
    from tools.environments.local import build_subprocess_env
    current = _receipt(stamp, provider)
    if current is None or not _stale(current):
        return
    remaining = deadline - time.monotonic()
    cooldown = min(300.0, max(60.0, 2 * current[1]))
    if remaining <= 0 or not _claim_attempt(lock, provider, cooldown, current[0]):
        return
    env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=False)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return
    result = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=remaining, check=False, env=env)
    if result.returncode:
        return
    while time.monotonic() < deadline:
        fresh = _receipt(stamp, provider)
        if fresh is not None and fresh[0] > current[0] and not _stale(fresh):
            return
        if not _wait(deadline):
            return


def before_pinned_request(provider: str, entry: Any) -> None:
    """Catch up a stale successful-pull receipt; caller then re-reads its pinned row.

    Missing receipts are old producers, not evidence of a long pull gap. A failed
    pull keeps existing request/401 recovery behavior. No token material is logged.
    """
    from agent.credential_relay_repull import is_relay_managed, pull_command
    from hermes_constants import get_hermes_home
    if not is_relay_managed(entry):
        return
    try:
        import fcntl
        stamp = Path(get_hermes_home()) / "state" / "oauth-relay-success.json"
        receipt = _receipt(stamp, provider)
        if receipt is None or not _stale(receipt):
            return
        cmd = pull_command()
        if not cmd:
            return
        deadline = time.monotonic() + _WAIT_S
        with (stamp.parent / "oauth-relay-repull.lock").open("a+", encoding="utf-8") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not _wait(deadline):
                        return
            _pull_locked(cmd, stamp, provider, deadline, lock)
    except (ImportError, OSError, ValueError, subprocess.SubprocessError):
        logger.debug("OAuth relay preflight unavailable; retaining reactive recovery")
